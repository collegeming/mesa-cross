#!/usr/bin/env python3
"""Static checks for the guest Wayland WSI forced-LINEAR patch (v2).

The patched file is cross-compiled, and the effect needs a Wayland compositor
plus a host GPU, so this checker asserts the structure instead:

  * the patch touches exactly one file and only inserts lines, so every
    pre-wants_linear line of wsi_wl_surface_create_swapchain is byte-for-byte
    the baseline;
  * the wants_linear gate is inside `if (f != NULL)` in the DRM branch, after
    the compositor/driver modifier list was read and before the
    num_modifier_lists bookkeeping, so a False predicate is the old path;
  * LINEAR is forced unconditionally: the two assignments sit after the warning
    branch closes, still inside the gate, so an empty list, a list without
    LINEAR and a list with LINEAR all end at exactly one LINEAR modifier;
  * the warning branch itself never assigns num_drm_modifiers or drm_modifiers
    and never returns, so an empty list cannot fall back to the original list;
  * num_drm_modifiers == 1 is paired with num_modifier_lists == 1;
  * the only logging is one rate-limited mesa_logw_once (no printf, no
    per-frame logging);
  * DRM_FORMAT_MOD_LINEAR comes from the include the file already has, and no
    include is added.
"""
import argparse
import re
import sys
from pathlib import Path

WAYLAND_REL = 'src/vulkan/wsi/wsi_common_wayland.c'
MARKER = 'wsi_wl_linear_modifier'

FAILS = []


def check(condition, message):
    if condition:
        print('PASS ' + message)
    else:
        FAILS.append(message)
        print('FAIL ' + message)


def region_between(text, start, end):
    begin = text.index(start)
    return text[begin:text.index(end, begin)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path, help='patched mesa source tree')
    parser.add_argument('--diff', type=Path, help='unified diff written by the patch script')
    args = parser.parse_args()
    root = args.root.resolve()

    text = (root / WAYLAND_REL).read_text(encoding='utf-8')

    # ------------------------------------------------------------ declaration
    decl = 'static const uint64_t wsi_wl_linear_modifier = DRM_FORMAT_MOD_LINEAR;'
    check(text.count(decl) == 1, 'one file-scope static const LINEAR modifier')
    check('\n' + decl in text, 'the static const starts at column 0 (file scope)')
    check(text.index(decl) < text.index('wsi_wl_surface_create_swapchain('),
          'the static const is declared before the function that uses it')
    check(text.count('DRM_FORMAT_MOD_LINEAR') == 2,
          'LINEAR appears twice: the static const and the advertised-list test')

    # ------------------------------------------------------- placement in DRM
    f_block = text.index('         if (f != NULL) {')
    read_len = text.index('            num_drm_modifiers = u_vector_length(&f->modifiers);')
    read_tail = text.index('            drm_modifiers = u_vector_tail(&f->modifiers);')
    gate = text.index('            if (wsi_device->wants_linear) {')
    book = text.index('            if (num_drm_modifiers > 0)\n'
                      '               drm_image_params.num_modifier_lists = 1;')
    check(f_block < read_len < read_tail < gate < book,
          'gate sits inside if (f != NULL), after the list read, before the bookkeeping')
    code_lines = [line for line in text.splitlines()
                  if not line.lstrip().startswith(('*', '/*', '//'))]
    wants_code = [line for line in code_lines if 'wsi_device->wants_linear' in line]
    check(len(wants_code) == 1 and 'if (wsi_device->wants_linear) {' in wants_code[0],
          'wants_linear is consulted in exactly one code line')

    # -------------------------------------------------------------- LINEAR use
    check(text.count('            if (wsi_device->wants_linear) {\n'
                     '               bool has_linear = false;\n') == 1,
          'the gate opens with a fresh has_linear flag')
    check('for (uint32_t i = 0; i < num_drm_modifiers; i++) {' in text,
          'the advertised list is scanned with the resolved length')
    check('if (drm_modifiers[i] == DRM_FORMAT_MOD_LINEAR) {' in text,
          'the scan looks for DRM_FORMAT_MOD_LINEAR')
    check(text.count('num_drm_modifiers = 1;') == 1,
          'the single-modifier assignment exists once')
    check(text.count('drm_modifiers = &wsi_wl_linear_modifier;') == 1,
          'drm_modifiers points at the static const once')

    # -------------------------------------------------- unconditional forcing
    # The assignments follow the warning branch's closing brace at the gate's
    # statement indentation, so nothing between the gate and them can skip them.
    tail = ('               }\n'
            '               num_drm_modifiers = 1;\n'
            '               drm_modifiers = &wsi_wl_linear_modifier;\n'
            '            }\n')
    check(text.count(tail) == 1,
          'LINEAR is assigned once, after the warning branch, inside the gate')
    check(text.index(tail, gate) > text.index('if (!has_linear) {', gate),
          'the force comes after the not-advertised warning')
    check('if (has_linear) {' not in text,
          'no positive has_linear branch can skip the force')
    gate_block = region_between(text, '            if (wsi_device->wants_linear) {',
                                '            }\n            if (num_drm_modifiers > 0)')
    check('return' not in gate_block and 'continue' not in gate_block,
          'the gate never leaves the function or the loop')
    check(gate_block.count('num_drm_modifiers = 1;') == 1 and
          gate_block.count('drm_modifiers = &wsi_wl_linear_modifier;') == 1,
          'both LINEAR assignments live in the gate body')

    # ------------------------------------------------------------- the warning
    warn = region_between(text, '               if (!has_linear) {',
                          '               }\n               num_drm_modifiers = 1;')
    check('mesa_logw_once(' in warn,
          'the not-advertised branch logs through mesa_logw_once')
    check('advertised no LINEAR modifier (count=%u); ' in warn,
          'the warning reports how many modifiers the compositor advertised')
    check('forcing LINEAR' in warn,
          'the warning states that LINEAR is forced anyway')
    check('num_drm_modifiers =' not in warn and 'drm_modifiers =' not in warn,
          'the warning branch leaves the list untouched')
    check(text.count('mesa_logw_once(') == 1, 'exactly one rate-limited log call')
    check('fprintf(' not in text and 'std::cout' not in text and 'puts(' not in text,
          'no printf-family logging')

    # -------------------------------------------------- original bookkeeping
    check('            if (num_drm_modifiers > 0)\n'
          '               drm_image_params.num_modifier_lists = 1;\n'
          '            else\n'
          '               drm_image_params.num_modifier_lists = 0;\n' in text,
          'num_modifier_lists still follows num_drm_modifiers')
    check(text.count('drm_image_params.num_modifier_lists') == 2,
          'num_modifier_lists keeps its two original assignments')
    check('            drm_image_params.num_modifiers = &num_drm_modifiers;\n'
          '            drm_image_params.modifiers = &drm_modifiers;\n' in text,
          'the pointers handed to wsi_swapchain_init are unchanged')
    check(text.count('result = wsi_swapchain_init(wsi_device, &chain->base, device,') == 1,
          'wsi_swapchain_init call is unchanged and unique')
    check(text.index('               num_drm_modifiers = 1;') < book,
          'the forced assignment precedes the num_modifier_lists decision')
    # num_drm_modifiers == 1 is strictly greater than zero, so the forced
    # selection always lands in the `num_modifier_lists = 1` arm.
    check('            if (num_drm_modifiers > 0)' in text,
          'the forced path reaches num_modifier_lists == 1')

    # ------------------------------------------------------------- includes
    check(text.count('#include "drm-uapi/drm_fourcc.h"') == 1,
          'drm_fourcc.h is included exactly once')
    check('#include <drm_fourcc.h>' not in text,
          'no duplicate system drm_fourcc.h include was added')

    # ------------------------------------------------------------------ diff
    if args.diff:
        diff_text = args.diff.read_text(encoding='utf-8')
        paths = set(re.findall(r'^diff --git a/(\S+) b/', diff_text, re.MULTILINE))
        check(paths == {WAYLAND_REL},
              f'the diff touches exactly {WAYLAND_REL} (got {sorted(paths)})')

        body = diff_text.splitlines()
        removed = [line for line in body
                   if line.startswith('-') and not line.startswith('---')]
        added = [line[1:] for line in body
                 if line.startswith('+') and not line.startswith('+++')]
        check(not removed,
              f'pure insertion: no original line is removed or rewritten (got {len(removed)})')
        check(any('if (wsi_device->wants_linear) {' in line for line in added),
              'the diff adds the wants_linear gate')
        check(sum(1 for line in added if 'mesa_logw_once(' in line) == 1,
              'the diff adds exactly one mesa_logw_once')
        check(not any(line.lstrip().startswith('#include') for line in added),
              'the diff adds no include')
        check(sum(1 for line in added if 'num_drm_modifiers = 1;' in line) == 1,
              'the diff adds the single-modifier assignment once')
        check(sum(1 for line in added if 'drm_modifiers = &wsi_wl_linear_modifier;' in line) == 1,
              'the diff adds the static-const pointer once')
        check(sum(1 for line in added if decl in line) == 1,
              'the diff adds the file-scope static const once')
        check(not any(re.search(r'\b(goto|abort|assert)\b', line) for line in added),
              'the inserted code adds no control-flow escape')

    if FAILS:
        print(f'\n{len(FAILS)} check(s) failed')
        return 1
    print('\nall guest wsi linear v2 checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
