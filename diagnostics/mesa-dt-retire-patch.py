"""Retire Wayland displaytarget cache entries when their drawable is destroyed.

Formal, Wayland-only fix for taowen/mesa
98f3d6229d61452cef80f8563af7c56ae599dc14.

The cache is keyed by the native wl_surface pointer, but a new wl_surface can
reuse the same address as soon as the old one is destroyed.  A displaytarget
must therefore stop being discoverable at drawable destroy time, even though
its Vulkan surface/swapchain may still be referenced by in-flight batches.
Teardown stays with the last resource reference.

Use --dry-run to print the diff without writing.  Every replacement is
validated; nothing is written unless all inputs and replacements match.
"""
import argparse
import difflib
import hashlib
from pathlib import Path


PINNED_INPUTS = {
    "src/gallium/frontends/dri/kopper.c":
        "85dd885424dc011db7a79180f889a1b8604ba282621bd53f68db090bbc2040c3",
    "src/gallium/drivers/zink/zink_kopper.h":
        "626c18cd95046f8b4d6da35a53ba2ddfbe0c7a88aabd4566f3541f2d8a311424",
    "src/gallium/drivers/zink/zink_kopper.c":
        "f815a4a0cb2a853ccc5d83820679e1ad8187054f89f0ba34c7f674d7f2cbe715",
}


def read_source(path):
    text = path.read_bytes().decode("utf-8")
    return text.replace("\r\n", "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    original = {}
    for name, digest in PINNED_INPUTS.items():
        text = read_source(args.source_root / name)
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == digest, (
            name, "expected pinned 98f3d6229d source")
        original[name] = text
    patched = original.copy()

    def replace(name, old, new):
        count = patched[name].count(old)
        assert count == 1, (name, old, count)
        patched[name] = patched[name].replace(old, new)

    frontend, header, source = PINNED_INPUTS

    # 1. Retire the target before the native window/surface can die.  This is
    # only cache membership; resource references still own teardown.
    replace(frontend, """kopper_destroy_drawable(struct dri_drawable *drawable)
{
""", """kopper_destroy_drawable(struct dri_drawable *drawable)
{
   /* Stop handing this target to a new generation that may reuse the same
    * wl_surface address.  The target stays alive until its last reference.
    */
   zink_kopper_retire_drawable(kopper_get_zink_screen(drawable->screen->base.screen),
                               &drawable->info);
""")

    # 2. Track the drawables that own a Wayland target.
    replace(header, "   void *loader_private;\n", """   void *loader_private;

   /* Opaque drawable-info pointers, not dereferenced; protected by
    * zink_screen::dt_lock.  Wayland only.
    */
   struct set *wayland_owners;
""")
    replace(header, """void
zink_kopper_displaytarget_destroy(struct zink_screen *screen, struct kopper_displaytarget *cdt);
""", """void
zink_kopper_displaytarget_destroy(struct zink_screen *screen, struct kopper_displaytarget *cdt);
void
zink_kopper_retire_drawable(struct pipe_screen *pscreen, const struct kopper_loader_info *info);
""")

    # 3. Drop the owner and, for the last owner, the cache entry.  Never
    # decrement the target reference or destroy Vulkan objects here.
    replace(source, """void
zink_kopper_deinit_displaytarget(struct zink_screen *screen, struct kopper_displaytarget *cdt)
""", """void
zink_kopper_retire_drawable(struct pipe_screen *pscreen, const struct kopper_loader_info *info)
{
#ifndef VK_USE_PLATFORM_WAYLAND_KHR
   (void)pscreen;
   (void)info;
#else
   if (info->bos.sType != VK_STRUCTURE_TYPE_WAYLAND_SURFACE_CREATE_INFO_KHR)
      return;

   struct zink_screen *screen = zink_screen(pscreen);
   struct kopper_displaytarget key = {0};
   key.info = *info;
   key.type = KOPPER_WAYLAND;

   simple_mtx_lock(&screen->dt_lock);
   struct hash_entry *he = screen->dts.table ? find_dt_entry(screen, &key) : NULL;
   if (he) {
      struct kopper_displaytarget *cdt = he->data;
      struct set_entry *owner = cdt->wayland_owners ?
         _mesa_set_search(cdt->wayland_owners, info) : NULL;
      if (owner) {
         _mesa_set_remove(cdt->wayland_owners, owner);
         if (!cdt->wayland_owners->entries)
            _mesa_hash_table_remove(&screen->dts, he);
      }
   }
   simple_mtx_unlock(&screen->dt_lock);
#endif
}

void
zink_kopper_deinit_displaytarget(struct zink_screen *screen, struct kopper_displaytarget *cdt)
""")

    # 4. Deinit by object identity: a retired target can be absent, or its key
    # can already belong to a new generation.
    replace(source, """   simple_mtx_lock(&screen->dt_lock);
   struct hash_entry *he = find_dt_entry(screen, cdt);
   assert(he);
   /* this deinits the registered entry, which should always be the "right" entry */
   cdt = he->data;
   _mesa_hash_table_remove(&screen->dts, he);
   simple_mtx_unlock(&screen->dt_lock);
""", """   simple_mtx_lock(&screen->dt_lock);
   struct hash_entry *he = NULL;
   if (cdt->type != KOPPER_WAYLAND || screen->dts.table)
      he = find_dt_entry(screen, cdt);
   /* A retired Wayland target may have no cache entry, or the same key may
    * already belong to a new generation.  Only remove this exact object.
    */
   if (cdt->type != KOPPER_WAYLAND)
      assert(he);
   if (he && he->data == cdt)
      _mesa_hash_table_remove(&screen->dts, he);
   simple_mtx_unlock(&screen->dt_lock);
""")
    replace(source, """   cdt->swapchain = cdt->old_swapchain = NULL;
   cdt->surface = VK_NULL_HANDLE;
}
""", """   cdt->swapchain = cdt->old_swapchain = NULL;
   cdt->surface = VK_NULL_HANDLE;
   if (cdt->wayland_owners) {
      _mesa_set_destroy(cdt->wayland_owners, NULL);
      cdt->wayland_owners = NULL;
   }
}
""")

    # 5. Register Wayland owners on both cache hit and new-target paths.
    replace(source, """      simple_mtx_unlock(&screen->dt_lock);
      if (he) {
         cdt = he->data;
         p_atomic_inc(&cdt->refcount);
         *stride = cdt->stride;
         return cdt;
      }
""", """      if (he && k.type == KOPPER_WAYLAND) {
         cdt = he->data;
         if (!_mesa_set_search(cdt->wayland_owners, info)) {
            if (!_mesa_set_add(cdt->wayland_owners, info)) {
               simple_mtx_unlock(&screen->dt_lock);
               return NULL;
            }
         }
         p_atomic_inc(&cdt->refcount);
         *stride = cdt->stride;
         simple_mtx_unlock(&screen->dt_lock);
         return cdt;
      }
      simple_mtx_unlock(&screen->dt_lock);
      if (he) {
         cdt = he->data;
         p_atomic_inc(&cdt->refcount);
         *stride = cdt->stride;
         return cdt;
      }
""")
    replace(source, """   cdt->refcount = 1;
   cdt->loader_private = (void*)loader_private;
   cdt->info = *info;

   enum pipe_format srgb""", """   cdt->refcount = 1;
   cdt->loader_private = (void*)loader_private;
   cdt->info = *info;
   init_dt_type(cdt);
   if (cdt->type == KOPPER_WAYLAND) {
      cdt->wayland_owners = _mesa_set_create(NULL, _mesa_hash_pointer, _mesa_key_pointer_equal);
      if (!cdt->wayland_owners)
         goto out;
      if (!_mesa_set_add(cdt->wayland_owners, info))
         goto out;
   }

   enum pipe_format srgb""")

    # 6. Failure path must release the owner set too.
    replace(source, """//moar cleanup
out:
   FREE(cdt);
   return NULL;
""", """//moar cleanup
out:
   if (cdt->wayland_owners)
      _mesa_set_destroy(cdt->wayland_owners, NULL);
   FREE(cdt);
   return NULL;
""")

    for name in original:
        print("".join(difflib.unified_diff(
            original[name].splitlines(keepends=True),
            patched[name].splitlines(keepends=True),
            fromfile="a/" + name, tofile="b/" + name)), end="")
    if not args.dry_run:
        for name, text in patched.items():
            (args.source_root / name).write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
