#!/usr/bin/env python3
"""Patch only the pinned ResourceTracker; assert the source-level regression cases."""
import argparse
import difflib
import hashlib
from pathlib import Path

REL = "src/gfxstream/guest/vulkan_enc/ResourceTracker.cpp"
BASE_SHA256 = "a8bf4c14c20bf98c0d0c95fe8c7ab6cabce2eff6f1499a302dc67832a4cce1a7"
GUARD = """    if (extImgCiPtr &&
        (extImgCiPtr->handleTypes & VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT)) {"""
OLD_EXPR = """(drmFmtMod && drmFmtMod->drmFormatModifier == DRM_FORMAT_MOD_LINEAR) ||
                    std::any_of(
                        drmFmtModList->pDrmFormatModifiers,
                        drmFmtModList->pDrmFormatModifiers + drmFmtModList->drmFormatModifierCount,
                        [](const uint64_t mod) { return mod == DRM_FORMAT_MOD_LINEAR; });"""
NEW_EXPR = """(drmFmtMod && drmFmtMod->drmFormatModifier == DRM_FORMAT_MOD_LINEAR) ||
                    (drmFmtModList && std::any_of(
                        drmFmtModList->pDrmFormatModifiers,
                        drmFmtModList->pDrmFormatModifiers + drmFmtModList->drmFormatModifierCount,
                        [](const uint64_t mod) { return mod == DRM_FORMAT_MOD_LINEAR; }));"""


def enclosing_dma_guards(text, position):
    """Brace-depth source assertion, confined to this pinned, simple function region."""
    result = 0
    start = 0
    while (start := text.find(GUARD, start)) >= 0:
        opening = start + len(GUARD) - 1
        depth = 1
        end = opening + 1
        while depth:
            depth += (text[end] == "{") - (text[end] == "}")
            end += 1
        result += opening < position < end
        start = end
    return result


def transform(before):
    assert hashlib.sha256(before).hexdigest() == BASE_SHA256, "Wrong pinned source"
    text = before.decode()
    a = text.index("        const VkImageDrmFormatModifierExplicitCreateInfoEXT* drmFmtMod =", text.index("VkResult ResourceTracker::on_vkCreateImage("))
    b = text.index("\n        // Cross-process dma-buf consumers", a)
    block = text[a:b]
    assert enclosing_dma_guards(text, a) == 1, "Baseline modifier block must reproduce DMA-only bug"
    assert block.count(OLD_EXPR) == 1
    block = block.replace(OLD_EXPR, NEW_EXPR)
    # Close only the scanout guard; process modifiers for every handle type; reopen
    # the same guard for the existing DMA-only OPTIMAL fallback. Nothing is stripped.
    moved = "\n".join(line[4:] if line.startswith("    ") else line for line in block.split("\n"))
    replacement = "    }\n\n    // Modifier pNext handling is independent of the external memory handle type.\n" + moved + "\n" + GUARD
    patched = text[:a] + replacement + text[b:]
    return patched.encode()


def check(before, after):
    old = before.decode()
    new = after.decode()
    begin = new.index("VkResult ResourceTracker::on_vkCreateImage(")
    end = new.index("#ifdef VK_USE_PLATFORM_ANDROID_KHR", begin)
    region = new[begin:end]
    for token in ("const VkImageDrmFormatModifierExplicitCreateInfoEXT* drmFmtMod =", "if (doImageDrmFormatModifierEmulation(physicalDevice))", "vk_append_struct(&structChainIter, &localDrmFormatModifierInfo);", "vk_append_struct(&structChainIter, &localDrmFormatModifierList);"):
        assert enclosing_dma_guards(region, region.index(token)) == 0, token
    for token in ("if (wsiImageCi && wsiImageCi->scanout)", "if (localCreateInfo.tiling == VK_IMAGE_TILING_OPTIMAL)"):
        assert enclosing_dma_guards(region, region.index(token)) == 1, token
    assert "(drmFmtModList && std::any_of(" in region
    # Restore the exact pre-patch expression and whitespace of the moved block.
    old_a = old.index("        const VkImageDrmFormatModifierExplicitCreateInfoEXT* drmFmtMod =", old.index("VkResult ResourceTracker::on_vkCreateImage("))
    old_b = old.index("\n        // Cross-process dma-buf consumers", old_a)
    new_a = new.index("    const VkImageDrmFormatModifierExplicitCreateInfoEXT* drmFmtMod =", begin)
    new_b = new.index("\n" + GUARD, new_a)
    restored = "\n".join("    " + line if line else line for line in new[new_a:new_b].split("\n"))
    assert restored.replace(NEW_EXPR, OLD_EXPR) == old[old_a:old_b]
    # Scanout and fallback bodies, external-memory pNext preservation, and all
    # source outside the moved region must remain byte-for-byte unchanged.
    prefix = old[:old_a]
    assert new.startswith(prefix + "    }\n\n    // Modifier pNext handling is independent of the external memory handle type.\n")
    assert new[new_b:] == "\n" + GUARD + old[old_b:]
    assert "localExtImgCi = vk_make_orphan_copy(*extImgCiPtr);" in region
    assert "vk_append_struct(&structChainIter, &localExtImgCi);" in region
    print("PASS baseline: modifier handling incorrectly enclosed in DMA_BUF guard")
    print("PASS patched: OPAQUE_FD/no-external/DMA_BUF all reach modifier handling")
    print("PASS native modifier support: explicit and list pNext forwarding retained")
    print("PASS emulation: LINEAR accepted only if supplied; non-LINEAR rejection retained")
    print("PASS explicit non-LINEAR without list: short-circuit null guard present")
    print("PASS DMA_BUF-only scanout and OPTIMAL fallback bodies unchanged")
    print("PASS external-memory sharing pNext retained; no global FORCE-LINEAR change")
    print("BASE_SOURCE_SHA256=" + hashlib.sha256(before).hexdigest())
    print("PATCHED_SOURCE_SHA256=" + hashlib.sha256(after).hexdigest())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--check-against", type=Path)
    parser.add_argument("--diff", type=Path)
    args = parser.parse_args()
    if args.check_against:
        before = args.check_against.read_bytes()
        after = args.source.read_bytes()
        assert after == transform(before), "Patched source changed unexpectedly"
    else:
        before = args.source.read_bytes()
        after = transform(before)
    check(before, after)
    if args.diff:
        args.diff.write_text("".join(difflib.unified_diff(before.decode().splitlines(True), after.decode().splitlines(True), fromfile="a/" + REL, tofile="b/" + REL)), encoding="utf-8", newline="\n")
    if not args.check_against:
        args.source.write_bytes(after)


if __name__ == "__main__":
    main()
