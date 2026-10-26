// SPDX-License-Identifier: AGPL-3.0-only
// Compatibility adapter confined to the headless helper's process.
#include <mach-o/dyld.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
static int studio_executable(char *buf, uint32_t *size) {
 const char *path=getenv("H2D_STUDIO_EXECUTABLE");
 if(!path) return _NSGetExecutablePath(buf,size);
 uint32_t needed=(uint32_t)strlen(path)+1;
 if(*size<needed){*size=needed;return -1;}
 memcpy(buf,path,needed);return 0;
}
__attribute__((used)) static struct {const void *replacement;const void *original;} mapping
__attribute__((section("__DATA,__interpose"))) = {(const void*)studio_executable,(const void*)_NSGetExecutablePath};
