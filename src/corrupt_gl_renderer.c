#define _GNU_SOURCE
#include "vk_rt5070.h"

#include <locale.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <X11/Xlib.h>
#include <epoxy/gl.h>
#include <epoxy/glx.h>
#include <mpv/client.h>
#include <mpv/render_gl.h>

typedef struct {
    Display *dpy;
    Window parent_win;
    Window gl_win;
    Colormap cmap;
    GLXContext glctx;
    int width;
    int height;

    mpv_handle *mpv;
    mpv_render_context *mpv_gl;
    GLuint video_fbo;
    GLuint video_tex;
    int video_w;
    int video_h;

    /* GPU Ping-Pong Conway's Game of Life FBOs (640x360) */
    GLuint life_fbo[2];
    GLuint life_ping[2];
    int life_idx;
    int life_w;
    int life_h;
    GLuint life_prog;
    GLint lu_prev_life;
    GLint lu_rift_tex;
    GLint lu_life_res;
    GLint lu_time;
    GLint lu_loads;
    GLint lu_mouse;
    GLint lu_win;
    GLint lu_win_count;
    GLint lu_extra;

    GLuint rift_tex;
    GLuint veil_tex;
    GLuint hud_tex;
    GLuint rt_gi_tex;

    /* Lazy-initialized Hardware RT-Core Vulkan 1.3 Raytracer */
    VkRT5070 *vk_rt;
    int vk_rt_attempted;
    char spv_path[1024];
    int rt_w;
    int rt_h;
    int life_step_counter;
    uint16_t *rt_half_buf;
    uint8_t *life_read_buf;

    GLuint prog;
    GLuint vao;

    GLint u_video_tex;
    GLint u_rift_tex;
    GLint u_life_tex;
    GLint u_veil_tex;
    GLint u_hud_tex;
    GLint u_rt_gi_tex;
    GLint u_res;
    GLint u_life_grid;
    GLint u_time;
    GLint u_loads;
    GLint u_mouse;
    GLint u_win;
    GLint u_win_count;
    GLint u_extra;

    int win_count;
    float win_rects[32];
    float extra[4];
} CorruptGL;

static void *get_proc_address(void *ctx, const char *name) {
    (void)ctx;
    return (void *)glXGetProcAddressARB((const GLubyte *)name);
}

static char *read_file_text(const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); return NULL; }
    long len = ftell(f);
    if (len <= 0) { fclose(f); return NULL; }
    rewind(f);
    char *buf = (char *)malloc((size_t)len + 1);
    if (!buf) { fclose(f); return NULL; }
    size_t n = fread(buf, 1, (size_t)len, f);
    fclose(f);
    buf[n] = '\0';
    return buf;
}

static GLuint compile_shader(GLenum type, const char *src) {
    GLuint s = glCreateShader(type);
    glShaderSource(s, 1, &src, NULL);
    glCompileShader(s);
    GLint ok = 0;
    glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[2048];
        glGetShaderInfoLog(s, sizeof(log), NULL, log);
        fprintf(stderr, "corrupt_gl shader compile error:\n%s\n", log);
        glDeleteShader(s);
        return 0;
    }
    return s;
}

static GLuint build_program(const char *vert_src, const char *frag_path) {
    char *frag_src = read_file_text(frag_path);
    if (!frag_src) {
        fprintf(stderr, "corrupt_gl: cannot read shader %s\n", frag_path);
        return 0;
    }
    GLuint vs = compile_shader(GL_VERTEX_SHADER, vert_src);
    GLuint fs = compile_shader(GL_FRAGMENT_SHADER, frag_src);
    free(frag_src);
    if (!vs || !fs) {
        if (vs) glDeleteShader(vs);
        if (fs) glDeleteShader(fs);
        return 0;
    }
    GLuint prog = glCreateProgram();
    glAttachShader(prog, vs);
    glAttachShader(prog, fs);
    glLinkProgram(prog);
    glDeleteShader(vs);
    glDeleteShader(fs);

    GLint linked = 0;
    glGetProgramiv(prog, GL_LINK_STATUS, &linked);
    if (!linked) {
        char log[2048];
        glGetProgramInfoLog(prog, sizeof(log), NULL, log);
        fprintf(stderr, "corrupt_gl link error (%s):\n%s\n", frag_path, log);
        glDeleteProgram(prog);
        return 0;
    }
    return prog;
}

static GLuint create_tex2d(GLint filter) {
    GLuint tex = 0;
    glGenTextures(1, &tex);
    glBindTexture(GL_TEXTURE_2D, tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, filter);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, filter);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    return tex;
}

static GLint get_array_uniform_loc(GLuint prog, const char *elem0_name, const char *base_name) {
    GLint loc = glGetUniformLocation(prog, elem0_name);
    if (loc < 0) {
        loc = glGetUniformLocation(prog, base_name);
    }
    return loc;
}

static void upload_shared_uniforms(
    GLint loc_win,
    GLint loc_win_count,
    GLint loc_extra,
    const CorruptGL *c
) {
    if (loc_win >= 0) {
        glUniform4fv(loc_win, 8, c->win_rects);
    }
    if (loc_win_count >= 0) {
        glUniform1i(loc_win_count, c->win_count);
    }
    if (loc_extra >= 0) {
        glUniform4f(loc_extra, c->extra[0], c->extra[1], c->extra[2], c->extra[3]);
    }
}

void *corrupt_gl_init(
    unsigned long parent_xid,
    int width,
    int height,
    const char *video_path,
    const char *frag_shader_path,
    const char *life_shader_path
) {
    Display *dpy = XOpenDisplay(NULL);
    if (!dpy) return NULL;
    int screen = DefaultScreen(dpy);

    int fb_attribs[] = {
        GLX_X_RENDERABLE, True,
        GLX_DRAWABLE_TYPE, GLX_WINDOW_BIT,
        GLX_RENDER_TYPE, GLX_RGBA_BIT,
        GLX_X_VISUAL_TYPE, GLX_TRUE_COLOR,
        GLX_RED_SIZE, 8,
        GLX_GREEN_SIZE, 8,
        GLX_BLUE_SIZE, 8,
        GLX_ALPHA_SIZE, 8,
        GLX_DOUBLEBUFFER, True,
        None
    };
    int fbcount = 0;
    GLXFBConfig *fbc = glXChooseFBConfig(dpy, screen, fb_attribs, &fbcount);
    if (!fbc || fbcount == 0) {
        XCloseDisplay(dpy);
        return NULL;
    }
    GLXFBConfig best_fbc = fbc[0];
    XFree(fbc);

    XVisualInfo *vi = glXGetVisualFromFBConfig(dpy, best_fbc);
    if (!vi) {
        XCloseDisplay(dpy);
        return NULL;
    }

    Window parent = parent_xid ? (Window)parent_xid : RootWindow(dpy, screen);
    Colormap cmap = XCreateColormap(dpy, RootWindow(dpy, screen), vi->visual, AllocNone);
    XSetWindowAttributes swa;
    memset(&swa, 0, sizeof(swa));
    swa.colormap = cmap;
    swa.border_pixel = 0;
    swa.override_redirect = True;

    Window gl_win = XCreateWindow(
        dpy, parent, 0, 0,
        (unsigned int)(width > 0 ? width : 1920),
        (unsigned int)(height > 0 ? height : 1080),
        0, vi->depth, InputOutput, vi->visual,
        CWColormap | CWBorderPixel | CWOverrideRedirect, &swa
    );
    XFree(vi);
    XMapWindow(dpy, gl_win);
    XSync(dpy, False);

    int ctx_attribs[] = {
        GLX_CONTEXT_MAJOR_VERSION_ARB, 3,
        GLX_CONTEXT_MINOR_VERSION_ARB, 3,
        GLX_CONTEXT_PROFILE_MASK_ARB, GLX_CONTEXT_CORE_PROFILE_BIT_ARB,
        None
    };
    GLXContext glctx = glXCreateContextAttribsARB(dpy, best_fbc, NULL, True, ctx_attribs);
    if (!glctx) {
        XDestroyWindow(dpy, gl_win);
        XFreeColormap(dpy, cmap);
        XCloseDisplay(dpy);
        return NULL;
    }
    glXMakeCurrent(dpy, gl_win, glctx);

    if (epoxy_has_glx_extension(dpy, screen, "GLX_EXT_swap_control")) {
        glXSwapIntervalEXT(dpy, gl_win, 0);
    }

    const char *vert_win_src =
        "#version 330 core\n"
        "out vec2 v_uv;\n"
        "void main() {\n"
        "    vec2 pos = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);\n"
        "    v_uv = vec2(pos.x, 1.0 - pos.y);\n"
        "    gl_Position = vec4(pos * 2.0 - 1.0, 0.0, 1.0);\n"
        "}\n";

    const char *vert_fbo_src =
        "#version 330 core\n"
        "out vec2 v_uv;\n"
        "void main() {\n"
        "    vec2 pos = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);\n"
        "    v_uv = pos;\n"
        "    gl_Position = vec4(pos * 2.0 - 1.0, 0.0, 1.0);\n"
        "}\n";

    GLuint prog = build_program(vert_win_src, frag_shader_path);
    GLuint life_prog = build_program(vert_fbo_src, life_shader_path);
    if (!prog || !life_prog) {
        if (prog) glDeleteProgram(prog);
        if (life_prog) glDeleteProgram(life_prog);
        glXMakeCurrent(dpy, None, NULL);
        glXDestroyContext(dpy, glctx);
        XDestroyWindow(dpy, gl_win);
        XFreeColormap(dpy, cmap);
        XCloseDisplay(dpy);
        return NULL;
    }

    setlocale(LC_NUMERIC, "C");
    mpv_handle *mpv = mpv_create();
    if (!mpv) {
        glDeleteProgram(prog);
        glDeleteProgram(life_prog);
        glXMakeCurrent(dpy, None, NULL);
        glXDestroyContext(dpy, glctx);
        XDestroyWindow(dpy, gl_win);
        XFreeColormap(dpy, cmap);
        XCloseDisplay(dpy);
        return NULL;
    }
    mpv_set_option_string(mpv, "vo", "libmpv");
    mpv_set_option_string(mpv, "hwdec", "no");
    mpv_set_option_string(mpv, "vd-lavc-threads", "2");
    mpv_set_option_string(mpv, "loop-file", "inf");
    mpv_set_option_string(mpv, "aid", "no");
    mpv_set_option_string(mpv, "vid", "1");
    mpv_set_option_string(mpv, "keepaspect", "no");
    mpv_set_option_string(mpv, "really-quiet", "yes");
    if (mpv_initialize(mpv) < 0) {
        mpv_terminate_destroy(mpv);
        glDeleteProgram(prog);
        glDeleteProgram(life_prog);
        glXMakeCurrent(dpy, None, NULL);
        glXDestroyContext(dpy, glctx);
        XDestroyWindow(dpy, gl_win);
        XFreeColormap(dpy, cmap);
        XCloseDisplay(dpy);
        return NULL;
    }

    mpv_opengl_init_params gl_init = { .get_proc_address = get_proc_address };
    int adv = 0;
    mpv_render_param rparams[] = {
        {MPV_RENDER_PARAM_API_TYPE, MPV_RENDER_API_TYPE_OPENGL},
        {MPV_RENDER_PARAM_OPENGL_INIT_PARAMS, &gl_init},
        {MPV_RENDER_PARAM_ADVANCED_CONTROL, &adv},
        {0}
    };
    mpv_render_context *mpv_gl = NULL;
    if (mpv_render_context_create(&mpv_gl, mpv, rparams) < 0) {
        mpv_terminate_destroy(mpv);
        glDeleteProgram(prog);
        glDeleteProgram(life_prog);
        glXMakeCurrent(dpy, None, NULL);
        glXDestroyContext(dpy, glctx);
        XDestroyWindow(dpy, gl_win);
        XFreeColormap(dpy, cmap);
        XCloseDisplay(dpy);
        return NULL;
    }

    const char *load_cmd[] = {"loadfile", video_path, NULL};
    mpv_command(mpv, load_cmd);

    CorruptGL *c = (CorruptGL *)calloc(1, sizeof(CorruptGL));
    c->dpy = dpy;
    c->parent_win = parent;
    c->gl_win = gl_win;
    c->cmap = cmap;
    c->glctx = glctx;
    c->width = width > 0 ? width : 1920;
    c->height = height > 0 ? height : 1080;
    c->mpv = mpv;
    c->mpv_gl = mpv_gl;
    c->prog = prog;
    c->life_prog = life_prog;
    c->video_w = 1280;
    c->video_h = 720;
    c->life_w = 640;
    c->life_h = 360;
    c->life_idx = 0;
    c->rt_w = 480;
    c->rt_h = 270;

    c->win_count = 0;
    memset(c->win_rects, 0, sizeof(c->win_rects));
    c->extra[0] = 0.0f;
    c->extra[1] = 0.0f;
    c->extra[2] = -1.0f;
    c->extra[3] = 0.0f;

    /* Record SPIR-V path for lazy Vulkan RT-Core init (do NOT open dGPU until requested!) */
    const char *home_dir = getenv("HOME");
    if (!home_dir || !home_dir[0]) home_dir = ".";
    const char *slash = strrchr(frag_shader_path, '/');
    if (slash) {
        size_t dir_len = (size_t)(slash - frag_shader_path);
        if (dir_len < sizeof(c->spv_path) - 32) {
            memcpy(c->spv_path, frag_shader_path, dir_len);
            c->spv_path[dir_len] = '\0';
            strcat(c->spv_path, "/corrupt-rt5070.spv");
        } else {
            snprintf(c->spv_path, sizeof(c->spv_path), "%s/.local/share/wallpaper-mode/corrupt-rt5070.spv", home_dir);
        }
    } else {
        snprintf(c->spv_path, sizeof(c->spv_path), "%s/.local/share/wallpaper-mode/corrupt-rt5070.spv", home_dir);
    }
    c->vk_rt = NULL;
    c->vk_rt_attempted = 0;
    c->rt_half_buf = (uint16_t *)calloc((size_t)c->rt_w * c->rt_h * 4, sizeof(uint16_t));
    c->life_read_buf = (uint8_t *)calloc((size_t)c->life_w * c->life_h * 4, sizeof(uint8_t));

    glXMakeCurrent(c->dpy, c->gl_win, c->glctx);

    glGenVertexArrays(1, &c->vao);

    c->video_tex = create_tex2d(GL_LINEAR);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, c->video_w, c->video_h, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
    glGenFramebuffers(1, &c->video_fbo);
    glBindFramebuffer(GL_FRAMEBUFFER, c->video_fbo);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, c->video_tex, 0);
    glClearColor(0.0f, 0.0f, 0.0f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);

    glGenFramebuffers(2, c->life_fbo);
    for (int i = 0; i < 2; ++i) {
        c->life_ping[i] = create_tex2d(GL_LINEAR);
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, c->life_w, c->life_h, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
        glBindFramebuffer(GL_FRAMEBUFFER, c->life_fbo[i]);
        glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, c->life_ping[i], 0);
        glClearColor(0.0f, 0.0f, 0.0f, 0.0f);
        glClear(GL_COLOR_BUFFER_BIT);
    }
    glBindFramebuffer(GL_FRAMEBUFFER, 0);

    unsigned char zero[4] = {0, 0, 0, 0};
    c->rift_tex = create_tex2d(GL_LINEAR);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, 1, 1, 0, GL_RGBA, GL_UNSIGNED_BYTE, zero);

    c->veil_tex = create_tex2d(GL_LINEAR);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, 1, 1, 0, GL_BGRA, GL_UNSIGNED_BYTE, zero);

    c->hud_tex = create_tex2d(GL_LINEAR);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, 1, 1, 0, GL_BGRA, GL_UNSIGNED_BYTE, zero);

    uint16_t zero_h[4] = {0, 0, 0, 0x3c00}; /* rgb=0, ao=1.0 */
    c->rt_gi_tex = create_tex2d(GL_LINEAR);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA16F, 1, 1, 0, GL_RGBA, GL_HALF_FLOAT, zero_h);

    glUseProgram(c->life_prog);
    c->lu_prev_life = glGetUniformLocation(c->life_prog, "u_prev_life");
    c->lu_rift_tex  = glGetUniformLocation(c->life_prog, "u_rift_tex");
    c->lu_life_res  = glGetUniformLocation(c->life_prog, "u_life_res");
    c->lu_time      = glGetUniformLocation(c->life_prog, "u_time");
    c->lu_loads     = glGetUniformLocation(c->life_prog, "u_loads");
    c->lu_mouse     = glGetUniformLocation(c->life_prog, "u_mouse");
    c->lu_win       = get_array_uniform_loc(c->life_prog, "u_win[0]", "u_win");
    c->lu_win_count = glGetUniformLocation(c->life_prog, "u_win_count");
    c->lu_extra     = glGetUniformLocation(c->life_prog, "u_extra");
    if (c->lu_prev_life >= 0) glUniform1i(c->lu_prev_life, 0);
    if (c->lu_rift_tex >= 0)  glUniform1i(c->lu_rift_tex,  1);
    upload_shared_uniforms(c->lu_win, c->lu_win_count, c->lu_extra, c);

    glUseProgram(c->prog);
    c->u_video_tex = glGetUniformLocation(c->prog, "u_video_tex");
    c->u_rift_tex  = glGetUniformLocation(c->prog, "u_rift_tex");
    c->u_life_tex  = glGetUniformLocation(c->prog, "u_life_tex");
    c->u_veil_tex  = glGetUniformLocation(c->prog, "u_veil_tex");
    c->u_hud_tex   = glGetUniformLocation(c->prog, "u_hud_tex");
    c->u_rt_gi_tex = glGetUniformLocation(c->prog, "u_rt_gi_tex");
    c->u_res       = glGetUniformLocation(c->prog, "u_res");
    c->u_life_grid = glGetUniformLocation(c->prog, "u_life_grid");
    c->u_time      = glGetUniformLocation(c->prog, "u_time");
    c->u_loads     = glGetUniformLocation(c->prog, "u_loads");
    c->u_mouse     = glGetUniformLocation(c->prog, "u_mouse");
    c->u_win       = get_array_uniform_loc(c->prog, "u_win[0]", "u_win");
    c->u_win_count = glGetUniformLocation(c->prog, "u_win_count");
    c->u_extra     = glGetUniformLocation(c->prog, "u_extra");

    if (c->u_video_tex >= 0) glUniform1i(c->u_video_tex, 0);
    if (c->u_rift_tex >= 0)  glUniform1i(c->u_rift_tex,  1);
    if (c->u_life_tex >= 0)  glUniform1i(c->u_life_tex,  2);
    if (c->u_veil_tex >= 0)  glUniform1i(c->u_veil_tex,  3);
    if (c->u_hud_tex >= 0)   glUniform1i(c->u_hud_tex,   4);
    if (c->u_rt_gi_tex >= 0) glUniform1i(c->u_rt_gi_tex, 5);
    upload_shared_uniforms(c->u_win, c->u_win_count, c->u_extra, c);

    return c;
}

int corrupt_gl_enable_vk_rt(void *handle, int enable) {
    CorruptGL *c = (CorruptGL *)handle;
    if (!c) return 0;
    if (!enable) {
        return c->vk_rt ? 1 : 0;
    }
    if (c->vk_rt) return 1;
    if (c->vk_rt_attempted) return 0;
    c->vk_rt_attempted = 1;
    c->vk_rt = vk_rt5070_init(c->rt_w, c->rt_h, c->spv_path);
    glXMakeCurrent(c->dpy, c->gl_win, c->glctx);
    return c->vk_rt ? 1 : 0;
}

const char *corrupt_gl_rt_device(void *handle) {
    CorruptGL *c = (CorruptGL *)handle;
    if (!c || !c->vk_rt) return "";
    return vk_rt5070_device_name(c->vk_rt);
}

void corrupt_gl_resize(void *handle, int width, int height) {
    CorruptGL *c = (CorruptGL *)handle;
    if (!c || width <= 0 || height <= 0) return;
    c->width = width;
    c->height = height;
    glXMakeCurrent(c->dpy, c->gl_win, c->glctx);
    XResizeWindow(c->dpy, c->gl_win, (unsigned int)width, (unsigned int)height);
    XSync(c->dpy, False);
}

void corrupt_gl_upload_rift(void *handle, int w, int h, const void *rgba_data) {
    CorruptGL *c = (CorruptGL *)handle;
    if (!c || !rgba_data) return;
    glXMakeCurrent(c->dpy, c->gl_win, c->glctx);
    glBindTexture(GL_TEXTURE_2D, c->rift_tex);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, rgba_data);

    if (c->vk_rt && c->extra[0] > 0.5f && c->extra[0] < 1.5f && w == 480 && h == 270) {
        vk_rt5070_upload_fields(c->vk_rt, w, h, rgba_data, 0, 0, NULL);
    }
}

void corrupt_gl_upload_veil(void *handle, int w, int h, const void *bgra_data) {
    CorruptGL *c = (CorruptGL *)handle;
    if (!c || !bgra_data) return;
    glXMakeCurrent(c->dpy, c->gl_win, c->glctx);
    glBindTexture(GL_TEXTURE_2D, c->veil_tex);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 4);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_BGRA, GL_UNSIGNED_BYTE, bgra_data);
}

void corrupt_gl_upload_hud(void *handle, int w, int h, const void *bgra_data) {
    CorruptGL *c = (CorruptGL *)handle;
    if (!c || !bgra_data) return;
    glXMakeCurrent(c->dpy, c->gl_win, c->glctx);
    glBindTexture(GL_TEXTURE_2D, c->hud_tex);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 4);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_BGRA, GL_UNSIGNED_BYTE, bgra_data);
}

void corrupt_gl_set_windows(void *handle, int count, const float *xywh_uv_rects) {
    CorruptGL *c = (CorruptGL *)handle;
    if (!c) return;
    if (count < 0 || !xywh_uv_rects) count = 0;
    else if (count > 8) count = 8;
    c->win_count = count;
    memset(c->win_rects, 0, sizeof(c->win_rects));
    if (count > 0) {
        memcpy(c->win_rects, xywh_uv_rects, (size_t)(count * 4) * sizeof(float));
    }
    glXMakeCurrent(c->dpy, c->gl_win, c->glctx);
    glUseProgram(c->life_prog);
    upload_shared_uniforms(c->lu_win, c->lu_win_count, -1, c);
    glUseProgram(c->prog);
    upload_shared_uniforms(c->u_win, c->u_win_count, -1, c);
}

void corrupt_gl_set_extra(
    void *handle,
    float raytrace,
    float dwell_sec,
    float no_carrier_y,
    float scar_remap_seed
) {
    CorruptGL *c = (CorruptGL *)handle;
    if (!c) return;
    c->extra[0] = raytrace;
    c->extra[1] = dwell_sec;
    c->extra[2] = no_carrier_y;
    c->extra[3] = scar_remap_seed;

    glXMakeCurrent(c->dpy, c->gl_win, c->glctx);
    glUseProgram(c->life_prog);
    if (c->lu_extra >= 0) {
        glUniform4f(c->lu_extra, raytrace, dwell_sec, no_carrier_y, scar_remap_seed);
    }
    glUseProgram(c->prog);
    if (c->u_extra >= 0) {
        glUniform4f(c->u_extra, raytrace, dwell_sec, no_carrier_y, scar_remap_seed);
    }
}

int corrupt_gl_trace_lights(
    void *handle,
    float time_sec,
    float aspect,
    float temporal_blend,
    const float *loads4,
    const float *mouse4,
    int emitter_count,
    const float *emitters_f8,
    int box_count,
    const float *boxes_f5
) {
    CorruptGL *c = (CorruptGL *)handle;
    if (!c || !c->vk_rt || !c->rt_half_buf) return -1;

    int rc = vk_rt5070_trace(
        c->vk_rt,
        time_sec,
        aspect,
        temporal_blend,
        loads4,
        mouse4,
        emitter_count,
        (const VkRTEmitter *)emitters_f8,
        box_count,
        (const VkRTBoxWall *)boxes_f5,
        c->rt_half_buf
    );
    if (rc == 0) {
        glXMakeCurrent(c->dpy, c->gl_win, c->glctx);
        glBindTexture(GL_TEXTURE_2D, c->rt_gi_tex);
        glPixelStorei(GL_UNPACK_ALIGNMENT, 2);
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA16F, c->rt_w, c->rt_h, 0, GL_RGBA, GL_HALF_FLOAT, c->rt_half_buf);
        return 0;
    }

    /* Faulted or timed out: retire Vulkan handle cleanly and fall back to GLSL RT */
    vk_rt5070_destroy(c->vk_rt);
    c->vk_rt = NULL;
    glXMakeCurrent(c->dpy, c->gl_win, c->glctx);
    return -1;
}

void corrupt_gl_step_life(
    void *handle,
    float time_sec,
    float cpu_load,
    float gpu_load,
    float ram_load,
    float burst,
    float mouse_uv_x,
    float mouse_uv_y,
    float mouse_speed
) {
    CorruptGL *c = (CorruptGL *)handle;
    if (!c) return;
    glXMakeCurrent(c->dpy, c->gl_win, c->glctx);

    int src_i = c->life_idx;
    int dst_i = 1 - src_i;

    glBindFramebuffer(GL_FRAMEBUFFER, c->life_fbo[dst_i]);
    glViewport(0, 0, c->life_w, c->life_h);
    glUseProgram(c->life_prog);

    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, c->life_ping[src_i]);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);

    glActiveTexture(GL_TEXTURE1);
    glBindTexture(GL_TEXTURE_2D, c->rift_tex);

    float aspect = (float)c->width / (float)(c->height > 0 ? c->height : 1);
    if (c->lu_life_res >= 0) glUniform2f(c->lu_life_res, (float)c->life_w, (float)c->life_h);
    if (c->lu_time >= 0)     glUniform1f(c->lu_time, time_sec);
    if (c->lu_loads >= 0)    glUniform4f(c->lu_loads, cpu_load, gpu_load, ram_load, burst);
    if (c->lu_mouse >= 0)    glUniform4f(c->lu_mouse, mouse_uv_x, mouse_uv_y, mouse_speed, aspect);
    upload_shared_uniforms(c->lu_win, c->lu_win_count, c->lu_extra, c);

    glBindVertexArray(c->vao);
    glDrawArrays(GL_TRIANGLES, 0, 3);

    /* Stage life buffer every 2nd step only when hardware Vulkan RT is actively tracing */
    c->life_step_counter++;
    if (c->vk_rt && c->life_read_buf && c->extra[0] > 0.5f && c->extra[0] < 1.5f && (c->life_step_counter & 1) == 0) {
        glPixelStorei(GL_PACK_ALIGNMENT, 1);
        glReadPixels(0, 0, c->life_w, c->life_h, GL_RGBA, GL_UNSIGNED_BYTE, c->life_read_buf);
        vk_rt5070_upload_fields(c->vk_rt, 0, 0, NULL, c->life_w, c->life_h, c->life_read_buf);
    }

    glBindTexture(GL_TEXTURE_2D, c->life_ping[dst_i]);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);

    c->life_idx = dst_i;
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
}

static void bind_and_draw_main(
    CorruptGL *c,
    float time_sec,
    float cpu_load,
    float gpu_load,
    float ram_load,
    float burst,
    float mouse_uv_x,
    float mouse_uv_y,
    float mouse_speed
) {
    glViewport(0, 0, c->width, c->height);
    glUseProgram(c->prog);

    glActiveTexture(GL_TEXTURE0); glBindTexture(GL_TEXTURE_2D, c->video_tex);
    glActiveTexture(GL_TEXTURE1); glBindTexture(GL_TEXTURE_2D, c->rift_tex);
    glActiveTexture(GL_TEXTURE2); glBindTexture(GL_TEXTURE_2D, c->life_ping[c->life_idx]);
    glActiveTexture(GL_TEXTURE3); glBindTexture(GL_TEXTURE_2D, c->veil_tex);
    glActiveTexture(GL_TEXTURE4); glBindTexture(GL_TEXTURE_2D, c->hud_tex);
    glActiveTexture(GL_TEXTURE5); glBindTexture(GL_TEXTURE_2D, c->rt_gi_tex);

    float aspect = (float)c->width / (float)(c->height > 0 ? c->height : 1);
    if (c->u_res >= 0)       glUniform2f(c->u_res, (float)c->width, (float)c->height);
    if (c->u_life_grid >= 0) glUniform2f(c->u_life_grid, (float)c->life_w, (float)c->life_h);
    if (c->u_time >= 0)      glUniform1f(c->u_time, time_sec);
    if (c->u_loads >= 0)     glUniform4f(c->u_loads, cpu_load, gpu_load, ram_load, burst);
    if (c->u_mouse >= 0)     glUniform4f(c->u_mouse, mouse_uv_x, mouse_uv_y, mouse_speed, aspect);
    upload_shared_uniforms(c->u_win, c->u_win_count, c->u_extra, c);

    glBindVertexArray(c->vao);
    glDrawArrays(GL_TRIANGLES, 0, 3);
}

void corrupt_gl_render(
    void *handle,
    float time_sec,
    float cpu_load,
    float gpu_load,
    float ram_load,
    float burst,
    float mouse_uv_x,
    float mouse_uv_y,
    float mouse_speed
) {
    CorruptGL *c = (CorruptGL *)handle;
    if (!c) return;
    glXMakeCurrent(c->dpy, c->gl_win, c->glctx);

    while (1) {
        mpv_event *ev = mpv_wait_event(c->mpv, 0);
        if (!ev || ev->event_id == MPV_EVENT_NONE) break;
    }

    uint64_t flags = mpv_render_context_update(c->mpv_gl);
    if (flags & MPV_RENDER_UPDATE_FRAME) {
        mpv_opengl_fbo fbo = {
            .fbo = (int)c->video_fbo,
            .w = c->video_w,
            .h = c->video_h,
            .internal_format = GL_RGBA8
        };
        int flip_y = 0;
        mpv_render_param rparams[] = {
            {MPV_RENDER_PARAM_OPENGL_FBO, &fbo},
            {MPV_RENDER_PARAM_FLIP_Y, &flip_y},
            {0}
        };
        mpv_render_context_render(c->mpv_gl, rparams);
    }

    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    bind_and_draw_main(c, time_sec, cpu_load, gpu_load, ram_load, burst, mouse_uv_x, mouse_uv_y, mouse_speed);

    glXSwapBuffers(c->dpy, c->gl_win);
    mpv_render_context_report_swap(c->mpv_gl);
}

int corrupt_gl_read_pixels(
    void *handle,
    float time_sec,
    float cpu_load,
    float gpu_load,
    float ram_load,
    float burst,
    float mouse_uv_x,
    float mouse_uv_y,
    float mouse_speed,
    void *out_rgba
) {
    CorruptGL *c = (CorruptGL *)handle;
    if (!c || !out_rgba) return -1;
    glXMakeCurrent(c->dpy, c->gl_win, c->glctx);

    GLuint cap_tex = create_tex2d(GL_NEAREST);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, c->width, c->height, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
    GLuint cap_fbo = 0;
    glGenFramebuffers(1, &cap_fbo);
    glBindFramebuffer(GL_FRAMEBUFFER, cap_fbo);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, cap_tex, 0);

    bind_and_draw_main(c, time_sec, cpu_load, gpu_load, ram_load, burst, mouse_uv_x, mouse_uv_y, mouse_speed);

    glPixelStorei(GL_PACK_ALIGNMENT, 1);
    glReadPixels(0, 0, c->width, c->height, GL_RGBA, GL_UNSIGNED_BYTE, out_rgba);

    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    glDeleteFramebuffers(1, &cap_fbo);
    glDeleteTextures(1, &cap_tex);
    return 0;
}

void corrupt_gl_destroy(void *handle) {
    CorruptGL *c = (CorruptGL *)handle;
    if (!c) return;
    if (c->vk_rt) {
        vk_rt5070_destroy(c->vk_rt);
        c->vk_rt = NULL;
    }
    free(c->rt_half_buf);
    free(c->life_read_buf);

    glXMakeCurrent(c->dpy, c->gl_win, c->glctx);
    if (c->mpv_gl) mpv_render_context_free(c->mpv_gl);
    if (c->mpv) mpv_terminate_destroy(c->mpv);
    if (c->video_fbo) glDeleteFramebuffers(1, &c->video_fbo);
    glDeleteFramebuffers(2, c->life_fbo);
    GLuint texs[7] = {
        c->video_tex, c->rift_tex, c->veil_tex, c->hud_tex,
        c->rt_gi_tex, c->life_ping[0], c->life_ping[1]
    };
    glDeleteTextures(7, texs);
    if (c->vao) glDeleteVertexArrays(1, &c->vao);
    if (c->prog) glDeleteProgram(c->prog);
    if (c->life_prog) glDeleteProgram(c->life_prog);
    glXMakeCurrent(c->dpy, None, NULL);
    glXDestroyContext(c->dpy, c->glctx);
    XDestroyWindow(c->dpy, c->gl_win);
    XFreeColormap(c->dpy, c->cmap);
    XCloseDisplay(c->dpy);
    free(c);
}
