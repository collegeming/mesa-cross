#!/usr/bin/env python3
"""Apply/check the 47ec18e "no detach on swapchain teardown" candidate (hook2).

Applies on top of the format-capacity, WSI lifecycle and wl_surface-gone patches:
only client/wsi_wayland.c changes. wl_teardown no longer queues attach(NULL)+commit
when a live layer surface loses its swapchain. That detach unmaps the layer surface;
the next attach without left/right anchors is InvalidSize in smithay ("width 0
requested without setting left and right anchors"). Buffers are still destroyed after
the bounded release wait, and the wl-gone hook still suppresses every request once
the app destroys the wl_surface, so the invalid-object path is unchanged.
"""
import argparse
import hashlib
import subprocess
from pathlib import Path

REVISION = "47ec18eaa9d9f83c359d56ba412a17d61ba59a66"
# The exact wl_surface-gone candidate (capacity + lifecycle + gone hook), LF canonical.
PRE = {
    "client/wsi.h": "fc09877eb41d179c00eb633d2f38a6fbaf7008c5a8199d80bc2b7aa00b15d2ba",
    "client/wsi.c": "ce0bee15dc432bb917481023adcc6861b13350a915a8ac7aa422a8aba7d1f96a",
    "client/wsi_wayland.c": "bc19fcd6035514d4d3a5e251f513c38b5783be0ad3ee976f55e2f542961bd241",
}
# The exact post-patch candidate (LF canonical), for --verify-candidate.
POST = {
    "client/wsi.h": "fc09877eb41d179c00eb633d2f38a6fbaf7008c5a8199d80bc2b7aa00b15d2ba",
    "client/wsi.c": "ce0bee15dc432bb917481023adcc6861b13350a915a8ac7aa422a8aba7d1f96a",
    "client/wsi_wayland.c": "ba29906f1a62e939d5e46833cbae0fbc17a17fe44b90ac196d28f5aae8a8f603",
}

WSI_WAYLAND = [
    (b"    /* Nothing attached first: the compositor has no buffer of ours left to read once the\n"
     b"     * wl_buffers go below. Skipped when the app destroyed the surface - that is exactly the\n"
     b"     * object a request must not name any more. */\n"
     b"    if (s->surface_wrapper && !vkb_surface_is_dead(sc->surface)) {\n"
     b"        /* A detach queued after the destroy is the same \"invalid object\": skipped once the\n"
     b"         * shim has marked the surface, and serialised with a present in flight. */\n"
     b"        pthread_mutex_lock(&sc->surface->present_mu);\n"
     b"        if (!__atomic_load_n(&sc->surface_gone, __ATOMIC_ACQUIRE)) {\n"
     b"            WSI_TRACE(\"detach wl_surface#%u, commit\", proxy_id(s->surface_wrapper));\n"
     b"            wl.proxy_marshal(s->surface_wrapper, WL_SURFACE_ATTACH, NULL, 0, 0);\n"
     b"            wl.proxy_marshal(s->surface_wrapper, WL_SURFACE_COMMIT);\n"
     b"            wl.display_flush(s->display);\n"
     b"        }\n"
     b"        pthread_mutex_unlock(&sc->surface->present_mu);\n"
     b"    }\n",
     b"    /* No detach: this wl_surface outlives the swapchain. A scale change rebuilds the swapchain\n"
     b"     * while the layer surface stays mapped; attach(NULL)+commit unmaps it, and the next attach\n"
     b"     * without left/right anchors is InvalidSize in smithay (\"width 0 requested without setting\n"
     b"     * left and right anchors\"). The buffers below still go after a bounded release wait, and\n"
     b"     * the wl-gone hook still suppresses every request once the app destroys the wl_surface. */\n"),
]

FILES = {
    "client/wsi.h": [],
    "client/wsi.c": [],
    "client/wsi_wayland.c": WSI_WAYLAND,
}


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def canonical(data):
    return data.replace(b"\r\n", b"\n")


def sha(data):
    return hashlib.sha256(canonical(data)).hexdigest()


def replace(data, pairs, name):
    for old, new in pairs:
        if data.count(old) != 1:
            raise SystemExit(f"Exact patch anchor missing or duplicated in {name}: {old[:120]!r}")
        data = data.replace(old, new)
    return data


def restore_newlines(data, like):
    newline = b"\r\n" if b"\r\n" in like else b"\n"
    return canonical(data).replace(b"\n", newline)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", type=Path)
    ap.add_argument("--check", action="store_true", help="Validate the wl_surface-gone pre-state without writing")
    ap.add_argument("--verify-candidate", action="store_true", help="Validate exactly the expected candidate without writing")
    args = ap.parse_args()
    if args.check and args.verify_candidate:
        ap.error("Select at most one check mode")
    root = args.source.resolve()
    if git(root, "rev-parse", "HEAD") != REVISION:
        raise SystemExit("Refusing source revision other than " + REVISION)
    have = {name: (root / name).read_bytes() for name in FILES}
    if args.verify_candidate:
        for name, expected in POST.items():
            if sha(have[name]) != expected:
                raise SystemExit("Candidate content mismatch: " + name)
        if git(root, "diff", "--name-only", "HEAD").splitlines() != sorted(FILES):
            raise SystemExit("Unexpected tracked changes in the candidate")
        print("Verified the exact no-detach candidate (capacity + lifecycle + wl-gone included)")
        return
    for name, expected in PRE.items():
        if sha(have[name]) != expected:
            raise SystemExit("Pre-state is not the exact wl_surface-gone candidate: " + name)
    expected = {name: replace(canonical(have[name]), FILES[name], name) for name in FILES}
    if args.check:
        print("Verified the wl_surface-gone pre-state; the no-detach patch applies on top")
        return
    for name, want in expected.items():
        if canonical(have[name]) == want:
            print(name, "already candidate")
            continue
        (root / name).write_bytes(restore_newlines(want, have[name]))
        print(name, "patched (sha256=" + hashlib.sha256((root / name).read_bytes()).hexdigest() + ")")
    changes = git(root, "diff", "--name-only", "HEAD").splitlines()
    if changes != sorted(FILES):
        raise SystemExit("Unexpected tracked changes: " + repr(changes))
    print("Applied the no-detach candidate")


if __name__ == "__main__":
    main()
