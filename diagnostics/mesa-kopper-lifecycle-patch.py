"""Add logging only to pinned Mesa; native Wayland pointers are never dereferenced."""
from pathlib import Path
import sys

path = Path(sys.argv[1]) / "src/gallium/drivers/zink/zink_kopper.c"
text = path.read_text()


def replace(old, new):
    global text
    assert text.count(old) == 1, (old, text.count(old))
    text = text.replace(old, new)


helper = r'''
#include <stdio.h>
#include <stdlib.h>

static void
kopper_lifecycle_trace(const char *event, struct zink_screen *screen,
                       const struct kopper_displaytarget *cdt, const void *cached)
{
   if (!getenv("MESA_KOPPER_LIFECYCLE_TRACE"))
      return;
   const void *key = NULL;
#ifdef VK_USE_PLATFORM_WAYLAND_KHR
   if (cdt->type == KOPPER_WAYLAND)
      key = ((const VkWaylandSurfaceCreateInfoKHR *)&cdt->info.bos)->surface;
#endif
   fprintf(stderr, "KOPPER_LIFE %s screen=%p cdt=%p key=%p refs=%u vk_surface=%#llx swapchain=%p vk_swapchain=%#llx cached=%p\n",
           event, (void *)screen, (const void *)cdt, key, p_atomic_read(&cdt->refcount),
           (unsigned long long)(uintptr_t)cdt->surface, (void *)cdt->swapchain,
           cdt->swapchain ? (unsigned long long)(uintptr_t)cdt->swapchain->swapchain : 0ULL, cached);
}
'''
replace('#include "zink_kopper.h"\n', '#include "zink_kopper.h"\n' + helper)
replace('p_atomic_inc(&cdt->refcount);\n         *stride = cdt->stride;',
        'p_atomic_inc(&cdt->refcount);\n         kopper_lifecycle_trace("HIT", screen, cdt, cdt);\n         *stride = cdt->stride;')
replace('         he = find_dt_entry(screen, &k);\n      }\n      simple_mtx_unlock',
        '''         he = find_dt_entry(screen, &k);
      }
#ifdef VK_USE_PLATFORM_WAYLAND_KHR
      if (!he && k.type == KOPPER_WAYLAND && getenv("MESA_KOPPER_LIFECYCLE_TRACE"))
         fprintf(stderr, "KOPPER_LIFE MISS screen=%p key=%p\\n", (void *)screen,
                 (void *)((const VkWaylandSurfaceCreateInfoKHR *)&k.info.bos)->surface);
#endif
      simple_mtx_unlock''')
replace('   if (update_swapchain(screen, cdt, width, height) != VK_SUCCESS)\n      goto out;',
        '   if (update_swapchain(screen, cdt, width, height) != VK_SUCCESS)\n      goto out;\n   kopper_lifecycle_trace("NEW", screen, cdt, NULL);')
replace('   struct hash_entry *he = find_dt_entry(screen, cdt);\n   assert(he);',
        '   struct hash_entry *he = find_dt_entry(screen, cdt);\n   kopper_lifecycle_trace("DEINIT_LOOKUP", screen, cdt, he ? he->data : NULL);\n   assert(he);')
replace('   cdt = he->data;\n   _mesa_hash_table_remove',
        '   cdt = he->data;\n   kopper_lifecycle_trace("CACHE_REMOVE", screen, cdt, he->data);\n   _mesa_hash_table_remove')
replace('''   if (!p_atomic_dec_zero(&cdt->refcount))
      return;
   zink_kopper_deinit_displaytarget(screen, cdt);
   FREE(cdt);''', '''   kopper_lifecycle_trace("UNREF_BEFORE", screen, cdt, NULL);
   if (!p_atomic_dec_zero(&cdt->refcount))
      return;
   kopper_lifecycle_trace("LAST_REF", screen, cdt, NULL);
   zink_kopper_deinit_displaytarget(screen, cdt);
   kopper_lifecycle_trace("FREE", screen, cdt, NULL);
   FREE(cdt);''')
path.write_text(text)
print("Applied Kopper diagnostics only:", path)
