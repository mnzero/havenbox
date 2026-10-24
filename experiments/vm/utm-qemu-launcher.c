#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

extern char **environ;

typedef int (*tool_main_fn)(int, const char *[]);
typedef int (*qemu_init_fn)(int, const char *[], const char *[]);
typedef void (*qemu_loop_fn)(void);
typedef void (*qemu_cleanup_fn)(void);

int main(int argc, const char *argv[]) {
    const char *app = getenv("HB02_UTM_APP");
    if (app == NULL || app[0] == '\0') {
        app = "/Applications/UTM.app";
    }

    const char *slash = strrchr(argv[0], '/');
    const char *program = slash == NULL ? argv[0] : slash + 1;
    const char *framework = strcmp(program, "qemu-img") == 0
        ? "qemu-img.framework/Versions/A/qemu-img"
        : "qemu-aarch64-softmmu.framework/Versions/A/qemu-aarch64-softmmu";
    size_t size = strlen(app) + strlen("/Contents/Frameworks/") + strlen(framework) + 1;
    char *library = malloc(size);
    if (library == NULL) {
        fputs("failed to allocate QEMU framework path\n", stderr);
        return 1;
    }
    snprintf(library, size, "%s/Contents/Frameworks/%s", app, framework);

    void *handle = dlopen(library, RTLD_LOCAL | RTLD_LAZY | RTLD_FIRST);
    if (handle == NULL) {
        fprintf(stderr, "failed to load %s: %s\n", library, dlerror());
        free(library);
        return 1;
    }
    free(library);

    tool_main_fn tool_main = dlsym(handle, "main");
    if (tool_main != NULL) {
        return tool_main(argc, argv);
    }

    qemu_init_fn qemu_init = dlsym(handle, "qemu_init");
    qemu_loop_fn qemu_main_loop = dlsym(handle, "qemu_main_loop");
    qemu_cleanup_fn qemu_cleanup = dlsym(handle, "qemu_cleanup");
    if (qemu_init == NULL || qemu_main_loop == NULL || qemu_cleanup == NULL) {
        fprintf(stderr, "QEMU framework is missing lifecycle symbols: %s\n", dlerror());
        return 1;
    }
    qemu_init(argc, argv, (const char **)environ);
    qemu_main_loop();
    qemu_cleanup();
    return 0;
}
