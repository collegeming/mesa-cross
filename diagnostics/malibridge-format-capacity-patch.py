#!/usr/bin/env python3
"""Apply/check the isolated 47ec18e Wayland modifier-capacity candidate."""
import argparse
import hashlib
import subprocess
from pathlib import Path

REVISION = "47ec18eaa9d9f83c359d56ba412a17d61ba59a66"
BLOBS = {
    "client/wsi_wayland.c": "e43880a5d35f5f6be33e9619039fbf1af45c997d",
    "client/wsi.c": "9b897d79c6205afdd2c7c74100817aa1e0d41bdd",
    "CMakeLists.txt": "86f9dc01666fa40037c40df9c0c7870b46a1c978",
    "common/gen/vkb_gen_client.c": "d040cea43d9491966002245d5beb84ca8849ca5f",
}


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def canonical(data):
    return data.replace(b"\r\n", b"\n")


def blob_hash(data):
    data = canonical(data)
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def candidate(data):
    newline = b"\r\n" if b"\r\n" in data else b"\n"
    data = canonical(data)
    replacements = [
        (b"dmabuf_mods[256];", b"dmabuf_mods[2048];"),
        (b"if (s->nmods < 256) {", b"if (s->nmods < (int)(sizeof(s->dmabuf_mods) / sizeof(s->dmabuf_mods[0]))) {"),
        (b"        s->nmods++;\n    }\n}\n\nstatic const void *dmabuf_listener[]", b"        s->nmods++;\n    } else {\n        static int told;\n        if (__atomic_fetch_add(&told, 1, __ATOMIC_RELAXED) == 0)\n            VKB_WARN(\"Wayland: dma-buf modifier table full (%u entries); later format/modifier pairs dropped\",\n                     (unsigned)(sizeof(s->dmabuf_mods) / sizeof(s->dmabuf_mods[0])));\n    }\n}\n\nstatic const void *dmabuf_listener[]"),
    ]
    for old, new in replacements:
        if data.count(old) != 1:
            raise SystemExit("Exact patch anchor missing or duplicated: " + repr(old))
        data = data.replace(old, new)
    return data.replace(b"\n", newline)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", type=Path)
    ap.add_argument("--check", action="store_true", help="Validate pristine baseline without writing")
    ap.add_argument("--verify-candidate", action="store_true", help="Validate exactly the expected candidate without writing")
    args = ap.parse_args()
    if args.check and args.verify_candidate:
        ap.error("Select at most one check mode")
    root = args.source.resolve()
    if git(root, "rev-parse", "HEAD") != REVISION:
        raise SystemExit("Refusing source revision other than " + REVISION)
    expected_path = "client/wsi_wayland.c"
    for name, expected in BLOBS.items():
        recorded = git(root, "rev-parse", "HEAD:" + name)
        if recorded != expected:
            raise SystemExit("Unexpected committed blob: " + name)
        data = (root / name).read_bytes()
        if args.verify_candidate and name == expected_path:
            pristine = subprocess.check_output(["git", "-C", str(root), "show", "HEAD:" + name])
            if canonical(data) != canonical(candidate(pristine)):
                raise SystemExit("Candidate content mismatch: " + name)
        elif blob_hash(data) != expected:
            raise SystemExit("Working-tree blob differs from baseline: " + name)
        print(name, "git-blob=" + recorded, "sha256=" + hashlib.sha256(data).hexdigest())
    changes = git(root, "diff", "--name-only", "HEAD").splitlines()
    allowed = [expected_path] if args.verify_candidate else []
    if changes != allowed:
        raise SystemExit("Unexpected tracked changes: " + repr(changes))
    if not args.check and not args.verify_candidate:
        path = root / expected_path
        path.write_bytes(candidate(path.read_bytes()))
        print("Applied client-only candidate: capacity 2048, derived bound, once-per-process overflow warning")
    else:
        print("Verified", "candidate" if args.verify_candidate else "pristine baseline")


if __name__ == "__main__":
    main()
