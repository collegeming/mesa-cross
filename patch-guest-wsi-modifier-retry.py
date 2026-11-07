#!/usr/bin/env python3
"""Retry a failed Wayland swapchain create once with a single LINEAR modifier.

The guest picks its DRM format modifiers from the compositor's dma-buf
feedback. On this stack (gfxstream guest ICD + Mali host driver) the driver can
reject the advertised scanout modifier at image creation time. Mesa then
returns VK_ERROR_INVALID_DRM_FORMAT_MODIFIER_PLANE_LAYOUT_EXT (or
VK_ERROR_INVALID_EXTERNAL_HANDLE when the export fails) from
vkCreateSwapchainKHR, while the application keeps its old swapchain handle.
vkcube 1.4.363 ignores that error (its assert is compiled out), destroys the
old swapchain and then asks the destroyed handle for its images, which is the
stale-image path the G1/G2 build was instrumented for.

F1: wsi_wl_surface_create_swapchain becomes a thin wrapper around
wsi_wl_surface_create_swapchain_impl(..., bool force_linear). When the first
call fails with one of the two modifier/handle errors above, the wrapper logs
one warning and repeats the call with a single DRM_FORMAT_MOD_LINEAR entry.
Only the guest WSI file changes; no host code, no G1/G2 semantics, no new
include. A second failure is returned unchanged.

The script asserts the sha256 of the pinned cecc0e96 file, every anchor
exactly once, and the post-patch shape. Any mismatch aborts the build.
"""
import argparse
import difflib
import hashlib
from pathlib import Path

WSI_REL = 'src/vulkan/wsi/wsi_common_wayland.c'

# wsi_common_wayland.c at Droid-VM/mesa cecc0e96 (not touched by earlier patches).
BASE_SHA256 = 'a21bbfa8b008cd79df1895d5704c64a9fb766df2059dcb8f52e4ce74ca7c4835'
MARKER = 'wsi_wl_linear_modifier'

# --- anchor 1: rename the definition and add the file-scope LINEAR constant ---
SIG_OLD = '''static VkResult
wsi_wl_surface_create_swapchain(VkIcdSurfaceBase *icd_surface,
                                VkDevice device,
                                struct wsi_device *wsi_device,
                                const VkSwapchainCreateInfoKHR* pCreateInfo,
                                const VkAllocationCallbacks* pAllocator,
                                struct wsi_swapchain **swapchain_out)
{
'''

SIG_NEW = '''/* The compositor's dma-buf feedback can advertise scanout modifiers that the
 * driver rejects at image creation time. Retrying with this single entry keeps
 * the guest-side drm_modifier and wl_buffer metadata consistent with the
 * allocation the host is asked to make, without giving up tiled layouts for
 * swapchains the driver accepts. File scope keeps the pointer valid while
 * wsi_swapchain_init copies the list. */
static const uint64_t wsi_wl_linear_modifier = DRM_FORMAT_MOD_LINEAR;

static VkResult
wsi_wl_surface_create_swapchain_impl(VkIcdSurfaceBase *icd_surface,
                                     VkDevice device,
                                     struct wsi_device *wsi_device,
                                     const VkSwapchainCreateInfoKHR* pCreateInfo,
                                     const VkAllocationCallbacks* pAllocator,
                                     struct wsi_swapchain **swapchain_out,
                                     bool force_linear)
{
'''

# --- anchor 2/3: modifier list selection inside the impl ---
MODIFIER_OLD = '''            num_drm_modifiers = u_vector_length(&f->modifiers);
            drm_modifiers = u_vector_tail(&f->modifiers);
'''

MODIFIER_NEW = '''            if (force_linear) {
               num_drm_modifiers = 1;
               drm_modifiers = &wsi_wl_linear_modifier;
            } else {
               num_drm_modifiers = u_vector_length(&f->modifiers);
               drm_modifiers = u_vector_tail(&f->modifiers);
            }
'''

# --- anchor 5: the wrapper goes right before wsi_wl_init_wsi ---
INIT_WSI_OLD = '''VkResult
wsi_wl_init_wsi(struct wsi_device *wsi_device,
'''

INIT_WSI_NEW = '''static VkResult
wsi_wl_surface_create_swapchain(VkIcdSurfaceBase *icd_surface,
                                VkDevice device,
                                struct wsi_device *wsi_device,
                                const VkSwapchainCreateInfoKHR* pCreateInfo,
                                const VkAllocationCallbacks* pAllocator,
                                struct wsi_swapchain **swapchain_out)
{
   VkResult result =
      wsi_wl_surface_create_swapchain_impl(icd_surface, device, wsi_device,
                                           pCreateInfo, pAllocator,
                                           swapchain_out, false);
   if (result == VK_ERROR_INVALID_DRM_FORMAT_MODIFIER_PLANE_LAYOUT_EXT ||
       result == VK_ERROR_INVALID_EXTERNAL_HANDLE) {
      mesa_logw_once("wsi_wayland: swapchain create failed (%d) with the "
                     "compositor modifier list; retrying with LINEAR",
                     (int)result);
      result = wsi_wl_surface_create_swapchain_impl(icd_surface, device,
                                                    wsi_device, pCreateInfo,
                                                    pAllocator, swapchain_out,
                                                    true);
   }
   if (result != VK_SUCCESS) {
      mesa_logw("wsi_wayland: swapchain create failed: result=%d extent=%ux%u "
                "format=%d", (int)result, pCreateInfo->imageExtent.width,
                pCreateInfo->imageExtent.height,
                (int)pCreateInfo->imageFormat);
   }
   return result;
}

VkResult
wsi_wl_init_wsi(struct wsi_device *wsi_device,
'''

# Anchors that must stay unique but are not modified: they pin the place the
# force_linear branch has to sit in and prove the renamed function is the one
# that creates the images.
KEEP_ANCHORS = [
    ('            if (num_drm_modifiers > 0)',
     'modifier list count check'),
    ('   result = wsi_swapchain_init(wsi_device, &chain->base, device,',
     'wsi_swapchain_init call'),
    ('   for (uint32_t i = 0; i < chain->base.image_count; i++) {\n'
     '      result = wsi_wl_image_init(chain, &chain->images[i],',
     'swapchain image creation loop'),
]


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise SystemExit(f'{label}: expected exactly one anchor, found {count}')
    return text.replace(old, new, 1)


def require_once(text, token, label):
    count = text.count(token)
    if count != 1:
        raise SystemExit(f'{label}: expected exactly one anchor, found {count}')
    return count


def transform(root):
    """Return {relative path: bytes} for every file the patch owns."""
    path = root / WSI_REL
    if not path.is_file():
        raise SystemExit(f'{WSI_REL}: missing')
    original = path.read_bytes()
    text = original.decode('utf-8')
    if MARKER in text:
        raise SystemExit(f'{WSI_REL}: already patched ({MARKER} present)')
    if sha256(original) != BASE_SHA256:
        raise SystemExit(f'{WSI_REL}: sha256 mismatch, expected the pinned '
                         'cecc0e96 file')

    text = replace_once(text, SIG_OLD, SIG_NEW, f'{WSI_REL}: create_swapchain signature')
    text = replace_once(text, MODIFIER_OLD, MODIFIER_NEW,
                        f'{WSI_REL}: modifier list selection')
    for anchor, label in KEEP_ANCHORS:
        require_once(text, anchor, f'{WSI_REL}: {label}')
    text = replace_once(text, INIT_WSI_OLD, INIT_WSI_NEW,
                        f'{WSI_REL}: retry wrapper')
    return {WSI_REL: text.encode('utf-8')}


def include_lines(text):
    return [line for line in text.splitlines() if line.startswith('#include')]


def assert_patch_contract(patched, base_text):
    if set(patched) != {WSI_REL}:
        raise SystemExit(f'unexpected patched file set: {sorted(patched)}')
    text = patched[WSI_REL].decode()
    if include_lines(text) != include_lines(base_text):
        raise SystemExit('include guard: the include set changed')
    checks = [
        ('static const uint64_t wsi_wl_linear_modifier = DRM_FORMAT_MOD_LINEAR;', 1,
         'file-scope LINEAR modifier'),
        ('wsi_wl_surface_create_swapchain_impl', 3, 'impl definition plus two calls'),
        ('bool force_linear', 1, 'impl parameter'),
        ('if (force_linear) {', 1, 'LINEAR selection branch'),
        ('mesa_logw_once', 1, 'retry warning'),
        ('"wsi_wayland: swapchain create failed (%d) with the "', 1, 'retry warning text'),
        ('"compositor modifier list; retrying with LINEAR"', 1, 'retry warning tail'),
        ('VK_ERROR_INVALID_DRM_FORMAT_MODIFIER_PLANE_LAYOUT_EXT', 1,
         'modifier rejection result'),
        ('VK_ERROR_INVALID_EXTERNAL_HANDLE', 1, 'handle rejection result'),
        ('"wsi_wayland: swapchain create failed: result=%d extent=%ux%u "', 1,
         'final failure text'),
        ('wsi->base.create_swapchain = wsi_wl_surface_create_swapchain;', 1,
         'WSI callback still points at the wrapper'),
        ('drm_image_params.modifiers = &drm_modifiers;', 1,
         'modifier params still fed to wsi_swapchain_init'),
    ]
    for token, expected, label in checks:
        found = text.count(token)
        if found != expected:
            raise SystemExit(f'{label}: expected {expected} occurrence(s), found {found}')
    if 'wsi_wl_surface_create_swapchain_impl(VkIcdSurfaceBase *icd_surface' not in text:
        raise SystemExit('impl definition missing')
    if 'result = wsi_wl_surface_create_swapchain_impl(icd_surface, device,\n' \
       '                                                    wsi_device, pCreateInfo,' not in text:
        raise SystemExit('LINEAR retry call missing')


def write_diff(patched, base, path):
    rel = WSI_REL
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

    base = {WSI_REL: (root / WSI_REL).read_bytes()}
    patched = transform(root)
    assert_patch_contract(patched, base[WSI_REL].decode())

    for rel, data in patched.items():
        (root / rel).write_bytes(data)

    if args.diff:
        write_diff(patched, base, args.diff)

    for rel, data in patched.items():
        print(f'PATCHED {rel} sha256={sha256(data)}')
    print('PASS F1: vkCreateSwapchainKHR retries once with DRM_FORMAT_MOD_LINEAR '
          'after a modifier/handle rejection')
    print('PASS F1: the retry is limited to INVALID_DRM_FORMAT_MODIFIER_PLANE_LAYOUT'
          '_EXT and INVALID_EXTERNAL_HANDLE')
    print('PASS F1: a second failure is returned unchanged, no loop')
    print('PASS scope: wsi_common_wayland.c only, no host code, no new file, no include')
    print('guest wsi modifier retry patched')


if __name__ == '__main__':
    main()
