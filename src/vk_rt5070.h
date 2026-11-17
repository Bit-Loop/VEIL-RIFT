#ifndef VK_RT5070_H
#define VK_RT5070_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct VkRT5070 VkRT5070;

typedef struct {
    float pos_radius[4]; /* x=uv_x, y=uv_y, z=height, w=area_radius */
    float color_int[4];  /* r, g, b, intensity */
} VkRTEmitter;

typedef struct {
    float x0, y0, x1, y1; /* UV coordinates [0..1] */
    float z_top;          /* Extrusion height (e.g. 0.055f) */
} VkRTBoxWall;

VkRT5070 *vk_rt5070_init(int gi_w, int gi_h, const char *comp_spv_path);
const char *vk_rt5070_device_name(const VkRT5070 *rt);

void vk_rt5070_upload_fields(
    VkRT5070 *rt,
    int rift_w, int rift_h, const void *rift_rgba,
    int life_w, int life_h, const void *life_rgba
);

/* Rebuilds dynamic BLAS+TLAS on RTX 5070 RT Cores, dispatches rayQuery compute,
 * and writes gi_w * gi_h * 4 IEEE-754 half-floats (GL_RGBA16F) into out_rgba16f. */
int vk_rt5070_trace(
    VkRT5070 *rt,
    float time_sec,
    float aspect,
    float temporal_blend,
    const float loads[4],
    const float mouse[4],
    int emitter_count,
    const VkRTEmitter *emitters,
    int dynamic_box_count,
    const VkRTBoxWall *dynamic_boxes,
    void *out_rgba16f
);

void vk_rt5070_destroy(VkRT5070 *rt);

#ifdef __cplusplus
}
#endif

#endif
