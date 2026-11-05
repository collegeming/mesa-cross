#!/usr/bin/env python3
"""Make MESA_VK_WSI_DEBUG=linear force LINEAR on the Wayland DRM swapchain.

wsi_device->wants_linear is set from WSI_DEBUG_LINEAR, but at cecc0e96 only the
CPU-image path consults it. The Wayland DRM path in wsi_wl_surface_create_swapchain
takes its modifier list from the compositor's dma-buf feedback (or from the
display's advertised format list) and passes it to wsi_swapchain_init unchanged.
The host driver may reject the modifier the guest picked: gfxstream + Mali
returns VK_ERROR_INVALID_DRM_FORMAT_MODIFIER_PLANE_LAYOUT_EXT from
vkCreateSwapchainKHR and zink then has no swapchain at all.

v1 only switched to LINEAR when the selected format already advertised it. A new
wl_surface receives its dma-buf feedback asynchronously, so quickly reopening a
panel can observe a format whose modifier vector is still empty; v1 then kept
the empty list, WSI created the swapchain with no modifier, the driver chose a
tiled default and the host rejected it. v2 therefore forces LINEAR
unconditionally:

  * when wsi_device->wants_linear is true and the DRM branch selected a format
    (`f != NULL`), the swapchain always gets exactly one
    DRM_FORMAT_MOD_LINEAR modifier and num_modifier_lists == 1, whatever the
    compositor advertised -- empty list, list without LINEAR, or a list with
    LINEAR;
  * if the advertised list is empty or lacks LINEAR, one rate-limited warning
    (mesa_logw_once) records that the compositor did not advertise it, and the
    code still forces LINEAR; it never falls back to the original list.

This patch touches only src/vulkan/wsi/wsi_common_wayland.c and inserts two
blocks, leaving every existing line byte-for-byte unchanged:

  * a file-scope `static const uint64_t wsi_wl_linear_modifier =
    DRM_FORMAT_MOD_LINEAR;`, so the pointer the code hands to
    wsi_swapchain_init stays valid for the whole process and is copied
    synchronously by the callee;
  * inside `if (f != NULL)` in the DRM branch, the wants_linear gate described
    above, placed after the compositor list has been read and before the
    num_modifier_lists bookkeeping.

With wants_linear false the inserted branch is a single false predicate and the
modifier list, num_modifier_lists and the pointers stored in the chain are
exactly what the unpatched code produced.

DRM_FORMAT_MOD_LINEAR comes from "drm-uapi/drm_fourcc.h", which the file
already includes; the patch adds no include.

Pinned source: Droid-VM/mesa cecc0e96d7193c72356a31abb0cc589e4fc0415e
  src/vulkan/wsi/wsi_common_wayland.c
    sha256 a21bbfa8b008cd79df1895d5704c64a9fb766df2059dcb8f52e4ce74ca7c4835
"""
import argparse
import difflib
import hashlib
from pathlib import Path

WAYLAND_REL = 'src/vulkan/wsi/wsi_common_wayland.c'
BASE_SHA256 = 'a21bbfa8b008cd79df1895d5704c64a9fb766df2059dcb8f52e4ce74ca7c4835'
MARKER = 'wsi_wl_linear_modifier'

# Insertion point 1: file scope, immediately before the swapchain constructor.
ANCHOR_DECL = '''static VkResult
wsi_wl_surface_create_swapchain(VkIcdSurfaceBase *icd_surface,
'''

DECL_INSERT = '''/* MESA_VK_WSI_DEBUG=linear sets wsi_device->wants_linear, but the Wayland DRM
 * path below builds its modifier list from the compositor's dma-buf feedback
 * and hands it to wsi_swapchain_init unchanged. The host driver may reject the
 * modifier the guest picked (gfxstream + Mali returns
 * VK_ERROR_INVALID_DRM_FORMAT_MODIFIER_PLANE_LAYOUT_EXT from
 * vkCreateSwapchainKHR), which leaves zink without a swapchain. A single LINEAR
 * entry keeps the guest-side drm_modifier/wl_buffer metadata consistent with the
 * allocation the host is asked to make. File scope keeps the pointer valid while
 * wsi_swapchain_init copies the list. The feedback for a new wl_surface arrives
 * asynchronously, so the modifier list can still be empty here; wants_linear
 * forces LINEAR regardless.
 */
static const uint64_t wsi_wl_linear_modifier = DRM_FORMAT_MOD_LINEAR;

'''

# Insertion point 2: after the compositor's list has been selected, before the
# num_modifier_lists/num_modifiers bookkeeping that the original code performs.
ANCHOR_BODY = '''            if (num_drm_modifiers > 0)
               drm_image_params.num_modifier_lists = 1;
'''

BODY_INSERT = '''            if (wsi_device->wants_linear) {
               bool has_linear = false;

               for (uint32_t i = 0; i < num_drm_modifiers; i++) {
                  if (drm_modifiers[i] == DRM_FORMAT_MOD_LINEAR) {
                     has_linear = true;
                     break;
                  }
               }
               if (!has_linear) {
                  /* A new wl_surface receives its dma-buf feedback
                   * asynchronously, so a rapidly reopened panel can see an
                   * empty list. Asking for no modifier lets the driver pick a
                   * tiled default the host rejects, so force LINEAR anyway. */
                  mesa_logw_once("wsi_wayland: wants_linear but the compositor "
                                 "advertised no LINEAR modifier (count=%u); "
                                 "forcing LINEAR",
                                 num_drm_modifiers);
               }
               num_drm_modifiers = 1;
               drm_modifiers = &wsi_wl_linear_modifier;
            }
'''

# Tokens that must exist exactly once in the patched file. The original file
# contains none of them, so a single hit proves the insertion happened once.
UNIQUE_TOKENS = (
    'static const uint64_t wsi_wl_linear_modifier = DRM_FORMAT_MOD_LINEAR;',
    '            if (wsi_device->wants_linear) {\n',
    '               }\n'
    '               num_drm_modifiers = 1;\n'
    '               drm_modifiers = &wsi_wl_linear_modifier;\n',
    'mesa_logw_once("wsi_wayland: wants_linear but the compositor "',
)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise SystemExit(f'{label}: expected exactly one anchor, found {count}')
    return text.replace(old, new, 1)


def already_patched(text):
    return all(text.count(token) == 1 for token in UNIQUE_TOKENS)


def transform(root):
    """Return the patched file bytes, after proving every anchor."""
    path = root / WAYLAND_REL
    if not path.is_file():
        raise SystemExit(f'{WAYLAND_REL}: missing')
    original = path.read_bytes()
    text = original.decode('utf-8')

    # Re-running on a tree this script already patched is a no-op: every anchor
    # and the pinned sha256 describe the pristine file, which is gone by then.
    if MARKER in text:
        if not already_patched(text):
            raise SystemExit(f'{WAYLAND_REL}: partially patched, refusing to guess')
        return original

    if sha256(original) != BASE_SHA256:
        raise SystemExit(f'{WAYLAND_REL}: sha256 mismatch, wrong pinned source')

    text = replace_once(text, ANCHOR_DECL, DECL_INSERT + ANCHOR_DECL,
                        f'{WAYLAND_REL}: linear modifier declaration')
    text = replace_once(text, ANCHOR_BODY, BODY_INSERT + ANCHOR_BODY,
                        f'{WAYLAND_REL}: wants_linear gate')

    # The insertion must not disturb any original line: both anchors are still
    # present, unchanged and in order.
    for anchor in (ANCHOR_DECL, ANCHOR_BODY):
        if text.count(anchor) != 1:
            raise SystemExit(f'anchor lost after patching: {anchor!r}')

    # Only one log call, and it is the rate-limited one in the warning branch.
    if text.count('mesa_logw_once(') != 1:
        raise SystemExit('expected exactly one mesa_logw_once')
    for token in ('fprintf(', 'puts(', 'std::cout'):
        if token in DECL_INSERT + BODY_INSERT:
            raise SystemExit(f'inserted code must not log through {token}')

    # The gate sits inside the `if (f != NULL)` block, after the compositor list
    # has been read and before the num_modifier_lists bookkeeping.
    pos_f = text.index('         if (f != NULL) {')
    pos_len = text.index('            num_drm_modifiers = u_vector_length(&f->modifiers);')
    pos_tail = text.index('            drm_modifiers = u_vector_tail(&f->modifiers);')
    pos_gate = text.index('            if (wsi_device->wants_linear) {')
    pos_book = text.index(ANCHOR_BODY)
    if not (pos_f < pos_len < pos_tail < pos_gate < pos_book):
        raise SystemExit('wants_linear gate is not between the list selection and the bookkeeping')

    # The force is unconditional: the two assignments sit after the warning
    # branch closes and still inside the gate, so an empty list, a list without
    # LINEAR and a list with LINEAR all end at the same single modifier.
    tail = ('               }\n'
            '               num_drm_modifiers = 1;\n'
            '               drm_modifiers = &wsi_wl_linear_modifier;\n'
            '            }\n')
    if text.count(tail) != 1:
        raise SystemExit('LINEAR assignments are not unconditional at the end of the gate')
    if 'if (has_linear) {' in text:
        raise SystemExit('the positive has_linear branch must not exist any more')
    if 'continue;' in BODY_INSERT or 'return' in BODY_INSERT:
        raise SystemExit('the gate must not leave the function')

    # The original bookkeeping is intact and still decides num_modifier_lists.
    needle = ('            if (num_drm_modifiers > 0)\n'
              '               drm_image_params.num_modifier_lists = 1;\n'
              '            else\n'
              '               drm_image_params.num_modifier_lists = 0;\n'
              '            drm_image_params.num_modifiers = &num_drm_modifiers;\n'
              '            drm_image_params.modifiers = &drm_modifiers;')
    if text.count(needle) != 1:
        raise SystemExit('original modifier bookkeeping changed')

    # No new include: drm_fourcc.h is already there and defines LINEAR.
    if text.count('drm-uapi/drm_fourcc.h') != 1 or '#include <drm_fourcc.h>' in text:
        raise SystemExit('drm_fourcc.h include must stay as it was')

    # Braces in the inserted block balance.
    block = BODY_INSERT
    if block.count('{') != block.count('}'):
        raise SystemExit('unbalanced braces in the wants_linear gate')

    return text.encode()


def write_diff(patched, base, path):
    before = base.decode('utf-8').splitlines(True)
    after = patched.decode('utf-8').splitlines(True)
    chunks = [f'diff --git a/{WAYLAND_REL} b/{WAYLAND_REL}\n']
    chunks.append(''.join(difflib.unified_diff(
        before, after, fromfile='a/' + WAYLAND_REL, tofile='b/' + WAYLAND_REL)))
    path.write_text(''.join(chunks), encoding='utf-8', newline='\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path, help='mesa source tree at cecc0e96')
    parser.add_argument('--diff', type=Path, help='write the unified diff here')
    args = parser.parse_args()
    root = args.root.resolve()

    base = (root / WAYLAND_REL).read_bytes()
    patched = transform(root)
    if patched == base:
        print(f'ALREADY PATCHED {WAYLAND_REL} sha256={sha256(base)}')
    else:
        (root / WAYLAND_REL).write_bytes(patched)
        print(f'PATCHED {WAYLAND_REL} sha256={sha256(patched)}')

    if args.diff:
        write_diff(patched, base, args.diff)

    print('PASS gated: only wsi_device->wants_linear changes the DRM modifier list')
    print('PASS forced: exactly DRM_FORMAT_MOD_LINEAR, one list, one modifier, always')
    print('PASS race: an empty or LINEAR-less advertised list still forces LINEAR')
    print('PASS warning: one rate-limited message when LINEAR was not advertised')
    print('PASS baseline: no original line changes; wants_linear false is the old path')
    print('guest wsi linear v2 patched')


if __name__ == '__main__':
    main()
