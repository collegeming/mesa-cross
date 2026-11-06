"""Retire Wayland cache entries without destroying live displaytargets.

Only for taowen/mesa 98f3d6229d61452cef80f8563af7c56ae599dc14,
after mesa-kopper-lifecycle-patch.py. Use --dry-run to print the exact diff
without writing source files. All three inputs are validated before any write.
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
        "4b8899f95168fa60c13c67b99434248bed9ec1406aee326a1cdd79acb63aaf67",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    original = {}
    for name, digest in PINNED_INPUTS.items():
        text = (args.source_root / name).read_text(encoding="utf-8")
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == digest, (
            name, "expected pinned source after lifecycle diagnostics")
        original[name] = text
    patched = original.copy()

    def replace(name, old, new):
        count = patched[name].count(old)
        assert count == 1, (name, old, count)
        patched[name] = patched[name].replace(old, new)

    frontend, header, source = PINNED_INPUTS
    replace(frontend, """kopper_destroy_drawable(struct dri_drawable *drawable)
{
""", """kopper_destroy_drawable(struct dri_drawable *drawable)
{
   /* Texture references have already been released by dri_destroy_drawable.
    * The cache may consequently already be gone; retirement is a no-op then.
    */
   zink_kopper_retire_drawable(kopper_get_zink_screen(drawable->screen->base.screen),
                               &drawable->info);
""")
    replace(header, "   void *loader_private;\n", """   void *loader_private;

   /* Unique drawable info addresses, not references; protected by dt_lock. */
   struct set *wayland_owners;
""")
    replace(header, """void
zink_kopper_displaytarget_destroy(struct zink_screen *screen, struct kopper_displaytarget *cdt);
""", """void
zink_kopper_displaytarget_destroy(struct zink_screen *screen, struct kopper_displaytarget *cdt);
void
zink_kopper_retire_drawable(struct pipe_screen *pscreen, const struct kopper_loader_info *info);
""")
    replace(source, """void
zink_kopper_deinit_displaytarget(struct zink_screen *screen, struct kopper_displaytarget *cdt)
""", r'''static void
kopper_owner_trace(const char *event, struct zink_screen *screen,
                   const struct kopper_displaytarget *cdt, const void *owner)
{
   if (getenv("MESA_KOPPER_LIFECYCLE_TRACE"))
      fprintf(stderr, "KOPPER_LIFE OWNER_%s screen=%p cdt=%p owner=%p owners=%u\n",
              event, (void *)screen, (const void *)cdt, owner, cdt->wayland_owners->entries);
}

void
zink_kopper_retire_drawable(struct pipe_screen *pscreen, const struct kopper_loader_info *info)
{
#ifdef VK_USE_PLATFORM_WAYLAND_KHR
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
      struct set_entry *owner = _mesa_set_search(cdt->wayland_owners, info);
      if (owner) {
         _mesa_set_remove(cdt->wayland_owners, owner);
         kopper_owner_trace("REMOVE", screen, cdt, info);
         if (!cdt->wayland_owners->entries) {
            kopper_lifecycle_trace("RETIRE", screen, cdt, he->data);
            _mesa_hash_table_remove(&screen->dts, he);
         }
      }
   }
   simple_mtx_unlock(&screen->dt_lock);
#endif
}

void
zink_kopper_deinit_displaytarget(struct zink_screen *screen, struct kopper_displaytarget *cdt)
''')
    replace(source, """   assert(he);
   /* this deinits the registered entry, which should always be the "right" entry */
   cdt = he->data;
   kopper_lifecycle_trace("CACHE_REMOVE", screen, cdt, he->data);
   _mesa_hash_table_remove(&screen->dts, he);
""", """   /* A retired Wayland target may be absent or replaced at the same key.
    * Always deinit the passed target, never the replacement.
    */
   if (cdt->type != KOPPER_WAYLAND)
      assert(he);
   if (he && he->data == cdt) {
      kopper_lifecycle_trace("CACHE_REMOVE", screen, cdt, he->data);
      _mesa_hash_table_remove(&screen->dts, he);
   }
""")
    replace(source, """   cdt->surface = VK_NULL_HANDLE;
}

static struct kopper_swapchain *
""", """   cdt->surface = VK_NULL_HANDLE;
   if (cdt->wayland_owners) {
      _mesa_set_destroy(cdt->wayland_owners, NULL);
      cdt->wayland_owners = NULL;
   }
}

static struct kopper_swapchain *
""")
    replace(source, """      simple_mtx_unlock(&screen->dt_lock);
      if (he) {
         cdt = he->data;
         p_atomic_inc(&cdt->refcount);
         kopper_lifecycle_trace("HIT", screen, cdt, cdt);
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
            kopper_owner_trace("ADD", screen, cdt, info);
         }
         p_atomic_inc(&cdt->refcount);
         kopper_lifecycle_trace("HIT", screen, cdt, cdt);
         *stride = cdt->stride;
         simple_mtx_unlock(&screen->dt_lock);
         return cdt;
      }
      simple_mtx_unlock(&screen->dt_lock);
      if (he) {
         cdt = he->data;
         p_atomic_inc(&cdt->refcount);
         kopper_lifecycle_trace("HIT", screen, cdt, cdt);
         *stride = cdt->stride;
         return cdt;
      }
""")
    replace(source, """   cdt->info = *info;

   enum pipe_format srgb""", """   cdt->info = *info;
   init_dt_type(cdt);
   if (cdt->type == KOPPER_WAYLAND) {
      cdt->wayland_owners = _mesa_set_create(NULL, _mesa_hash_pointer, _mesa_key_pointer_equal);
      if (!cdt->wayland_owners)
         goto out;
      simple_mtx_lock(&screen->dt_lock);
      bool added = _mesa_set_add(cdt->wayland_owners, info) != NULL;
      if (added)
         kopper_owner_trace("ADD", screen, cdt, info);
      simple_mtx_unlock(&screen->dt_lock);
      if (!added)
         goto out;
   }

   enum pipe_format srgb""")
    replace(source, """      _mesa_hash_table_insert(&screen->dts, wlsci->surface, cdt);
      break;
""", """      if (!_mesa_hash_table_insert(&screen->dts, wlsci->surface, cdt)) {
         simple_mtx_unlock(&screen->dt_lock);
         zink_kopper_deinit_displaytarget(screen, cdt);
         goto out;
      }
      break;
""")
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
