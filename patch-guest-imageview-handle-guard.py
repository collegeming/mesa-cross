#!/usr/bin/env python3
"""Stop the guest ICD from sending a non-wrapped VkImage to vkCreateImageView.

vkCreateImageView marshals VkImageViewCreateInfo.image through
get_host_u64_VkImage(), which reads the wrapper's `underlying` field at offset 0.
When the application hands in a handle that is not a live goldfish_VkImage
wrapper (a stack slot, a freed wrapper, some other object), the guest puts a
guest address on the wire, the host unbox misses, and the host image is null.

G1  on_vkCreateImageView rejects such a handle before anything is encoded:
    a handle that is not registered in info_VkImage (the table filled by
    CreateMapping when a create returns), or whose underlying handle is zero or
    below 2^48, returns VK_ERROR_VALIDATION_FAILED_EXT. The table lookup is a
    pointer comparison, so the guard never dereferences an unregistered handle;
    today's code would have read offset 0 of it during marshaling.

G2  GFXSTREAM_IMGID_DIAG=1 traces the handles themselves:
      IMGID view image=%p live=%d host=0x%llx tag=%u
      IMGID create %p underlying=0x%llx
      IMGID destroy %p underlying=0x%llx
    The three lines per image are enough to tell a stack slot from a heap
    wrapper and to see whether the handle was destroyed before the view was
    created. The environment is read once, on first use.

Both hooks live in ResourceTracker.cpp only: no new file, so the recipe's
patched-paths set is unchanged. The script runs after
patch-guest-readback-invalidate.py and asserts the post-patch sha256, so the
chain order is enforced. Every anchor must occur exactly once.
"""
import argparse
import difflib
import hashlib
from pathlib import Path

ENC = 'src/gfxstream/guest/vulkan_enc'
RT_CPP_REL = ENC + '/ResourceTracker.cpp'

# ResourceTracker.cpp after patch-guest-readback-invalidate.py (recipe baseline).
BASE_SHA256 = '9dd8803e40092b1450a65346d5d83b165724ca6b9c184ca60d32eb5be4b18443'
MARKER = 'GFXSTREAM_IMGID_DIAG'
ENV_NAME = 'GFXSTREAM_IMGID_DIAG'

INCLUDE_OLD = '#include <algorithm>\n'
INCLUDE_NEW = '#include <algorithm>\n#include <atomic>\n'

HELPER_SITE_OLD = '''static bool guestDiagEnabled() {
    static const bool enabled = [] {
        const char* v = getenv("GFXSTREAM_GUEST_DIAG");
        return v && v[0] && strcmp(v, "0") != 0;
    }();
    return enabled;
}
'''

HELPER_NEW = '''static bool guestDiagEnabled() {
    static const bool enabled = [] {
        const char* v = getenv("GFXSTREAM_GUEST_DIAG");
        return v && v[0] && strcmp(v, "0") != 0;
    }();
    return enabled;
}

// GFXSTREAM_IMGID_DIAG=1 traces the VkImage handles the application hands to
// vkCreateImageView and the ones the ICD hands back from vkCreateImage and
// releases in vkDestroyImage. The environment is read once.
static bool guestImgIdDiagEnabled() {
    static const bool enabled = [] {
        const char* v = getenv("GFXSTREAM_IMGID_DIAG");
        return v && v[0] && strcmp(v, "0") != 0 && strcmp(v, "off") != 0;
    }();
    return enabled;
}
'''

IMAGEVIEW_RETURN_OLD = '''    return enc->vkCreateImageView(device, &localCreateInfo, pAllocator, pView, true /* do lock */);
'''

IMAGEVIEW_RETURN_NEW = r'''    // VkImageViewCreateInfo.image is marshaled through get_host_u64_VkImage(), which
    // reads `underlying` out of a goldfish_VkImage wrapper. A handle that is not a live
    // wrapper of this ICD (a stack slot, a freed wrapper, any other object) puts a guest
    // address on the wire and the host unbox misses. Reject it here, before encoding.
    //
    // info_VkImage is filled by CreateMapping when a create returns and cleared by
    // DestroyMapping, so it is the guest-side table of live wrappers. The lookup only
    // compares pointers: an unregistered handle is never dereferenced. The two numeric
    // checks cover a live wrapper whose underlying value is unusable.
    if (pCreateInfo->image != VK_NULL_HANDLE) {
        bool liveWrapper;
        {
            std::lock_guard<std::recursive_mutex> lock(mLock);
            liveWrapper = info_VkImage.find(pCreateInfo->image) != info_VkImage.end();
        }
        const uint64_t hostImage =
            liveWrapper ? get_host_u64_VkImage(pCreateInfo->image) : 0;

        if (guestImgIdDiagEnabled()) {
            mesa_logi("IMGID view image=%p live=%d host=0x%llx tag=%u", (void*)pCreateInfo->image,
                      (int)liveWrapper, (unsigned long long)hostImage,
                      (unsigned)(hostImage >> 48));
        }

        if (!liveWrapper || hostImage == 0 || (hostImage >> 48) == 0) {
            static std::atomic<uint32_t> sRejected{0};
            if (sRejected.fetch_add(1, std::memory_order_relaxed) < 8) {
                mesa_loge("vkCreateImageView: non-boxed VkImage %p (live=%d, host handle 0x%llx)",
                          (void*)pCreateInfo->image, (int)liveWrapper,
                          (unsigned long long)hostImage);
            }
            return VK_ERROR_VALIDATION_FAILED_EXT;
        }
    }

    return enc->vkCreateImageView(device, &localCreateInfo, pAllocator, pView, true /* do lock */);
'''

CREATE_BLOCK_OLD = '''    if (res != VK_SUCCESS) return res;

    std::lock_guard<std::recursive_mutex> lock(mLock);

    auto it = info_VkImage.find(*pImage);
    if (it == info_VkImage.end()) return VK_ERROR_INITIALIZATION_FAILED;
'''

CREATE_BLOCK_NEW = '''    if (res != VK_SUCCESS) return res;

    if (guestImgIdDiagEnabled()) {
        mesa_logi("IMGID create %p underlying=0x%llx", (void*)*pImage,
                  (unsigned long long)get_host_u64_VkImage(*pImage));
    }

    std::lock_guard<std::recursive_mutex> lock(mLock);

    auto it = info_VkImage.find(*pImage);
    if (it == info_VkImage.end()) return VK_ERROR_INITIALIZATION_FAILED;
'''

DESTROY_BLOCK_OLD = '''    VkEncoder* enc = (VkEncoder*)context;
    enc->vkDestroyImage(device, image, pAllocator, true /* do lock */);
'''

DESTROY_BLOCK_NEW = '''    VkEncoder* enc = (VkEncoder*)context;
    if (guestImgIdDiagEnabled()) {
        mesa_logi("IMGID destroy %p underlying=0x%llx", (void*)image,
                  (unsigned long long)get_host_u64_VkImage(image));
    }
    enc->vkDestroyImage(device, image, pAllocator, true /* do lock */);
'''


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise SystemExit(f'{label}: expected exactly one anchor, found {count}')
    return text.replace(old, new, 1)


def transform(root):
    """Return {relative path: bytes} for every file the patch owns."""
    path = root / RT_CPP_REL
    if not path.is_file():
        raise SystemExit(f'{RT_CPP_REL}: missing')
    original = path.read_bytes()
    text = original.decode('utf-8')
    if MARKER in text:
        raise SystemExit(f'{RT_CPP_REL}: already patched ({MARKER} present)')
    if sha256(original) != BASE_SHA256:
        raise SystemExit(f'{RT_CPP_REL}: sha256 mismatch, expected the '
                         'guest-readback-invalidate patched tree')

    text = replace_once(text, INCLUDE_OLD, INCLUDE_NEW, f'{RT_CPP_REL}: include')
    text = replace_once(text, HELPER_SITE_OLD, HELPER_NEW, f'{RT_CPP_REL}: helper')
    text = replace_once(text, IMAGEVIEW_RETURN_OLD, IMAGEVIEW_RETURN_NEW,
                        f'{RT_CPP_REL}: vkCreateImageView guard')
    text = replace_once(text, CREATE_BLOCK_OLD, CREATE_BLOCK_NEW,
                        f'{RT_CPP_REL}: vkCreateImage trace')
    text = replace_once(text, DESTROY_BLOCK_OLD, DESTROY_BLOCK_NEW,
                        f'{RT_CPP_REL}: vkDestroyImage trace')
    return {RT_CPP_REL: text.encode('utf-8')}


def assert_patch_contract(patched):
    if set(patched) != {RT_CPP_REL}:
        raise SystemExit(f'unexpected patched file set: {sorted(patched)}')
    text = patched[RT_CPP_REL].decode()
    checks = [
        ('#include <atomic>\n', 1, 'atomic include'),
        ('bool guestImgIdDiagEnabled()', 1, 'diagnostic switch helper'),
        # One lookup already exists in the Android external-format branch.
        ('info_VkImage.find(pCreateInfo->image)', 2, 'live-wrapper lookup'),
        ('get_host_u64_VkImage(pCreateInfo->image)', 1, 'guard handle read'),
        # One rejection already exists in the tracked-buffer branch.
        ('return VK_ERROR_VALIDATION_FAILED_EXT;', 2, 'guard rejection'),
        ('"vkCreateImageView: non-boxed VkImage %p (live=%d, host handle 0x%llx)"', 1,
         'rejection line'),
        ('"IMGID view image=%p live=%d host=0x%llx tag=%u"', 1, 'view trace line'),
        ('"IMGID create %p underlying=0x%llx"', 1, 'create trace line'),
        ('"IMGID destroy %p underlying=0x%llx"', 1, 'destroy trace line'),
        # The comment above the helper and the getenv() call.
        (ENV_NAME, 2, 'environment variable'),
    ]
    for token, expected, label in checks:
        found = text.count(token)
        if found != expected:
            raise SystemExit(f'{label}: expected {expected} occurrence(s), found {found}')
    if 'return enc->vkCreateImageView(device, &localCreateInfo, pAllocator, pView, ' \
       'true /* do lock */);' not in text:
        raise SystemExit('vkCreateImageView encode call missing')
    if 'enc->vkDestroyImage(device, image, pAllocator, true /* do lock */);' not in text:
        raise SystemExit('vkDestroyImage encode call missing')


def write_diff(patched, base, path):
    rel = RT_CPP_REL
    before = base.get(rel, b'').decode().splitlines(True)
    after = patched[rel].decode().splitlines(True)
    chunks = [f'diff --git a/{rel} b/{rel}\n']
    chunks.append(''.join(difflib.unified_diff(
        before, after, fromfile='a/' + rel, tofile='b/' + rel)))
    path.write_text(''.join(chunks), encoding='utf-8', newline='\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path, help='mesa source tree at cecc0e96')
    parser.add_argument('--diff', type=Path, help='write the unified diff here')
    args = parser.parse_args()
    root = args.root.resolve()

    base = {RT_CPP_REL: (root / RT_CPP_REL).read_bytes()}
    patched = transform(root)
    assert_patch_contract(patched)

    for rel, data in patched.items():
        (root / rel).write_bytes(data)

    if args.diff:
        write_diff(patched, base, args.diff)

    for rel, data in patched.items():
        print(f'PATCHED {rel} sha256={sha256(data)}')
    print('PASS G1: vkCreateImageView rejects a VkImage that is not a live wrapper, '
          'before encoding')
    print('PASS G1: the registry lookup only compares pointers; unregistered handles '
          'are never dereferenced')
    print('PASS G2: GFXSTREAM_IMGID_DIAG=1 prints IMGID view/create/destroy handle lines')
    print('PASS scope: ResourceTracker.cpp only, no new file')
    print('guest imageview handle guard patched')


if __name__ == '__main__':
    main()
