#define _GNU_SOURCE
#include "vk_rt5070.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <vulkan/vulkan.h>

#define MAX_TRIANGLES 2048
#define MAX_VERTICES  (MAX_TRIANGLES * 3)
#define VK_FENCE_TIMEOUT_NS 80000000ULL /* 80ms strict non-blocking cap; never UINT64_MAX */
#define RIFT_STAGE_OFFSET   0ULL
#define LIFE_STAGE_OFFSET   (480ULL * 270ULL * 4ULL)

typedef struct {
    float meta[4];      /* x=time, y=aspect, z=emitter_count, w=temporal_blend */
    float loads[4];     /* cpu, gpu, ram, burst */
    float mouse[4];     /* mx, my, speed, dwell */
    VkRTEmitter emitters[24];
} SceneUBOData;

typedef struct {
    VkBuffer buf;
    VkDeviceMemory mem;
    void *mapped;
    VkDeviceSize size;
    VkDeviceAddress addr;
} RTBuf;

typedef struct {
    VkImage img;
    VkDeviceMemory mem;
    VkImageView view;
    int w;
    int h;
    VkFormat fmt;
    VkImageLayout layout;
} RTImg;

struct VkRT5070 {
    VkInstance instance;
    VkPhysicalDevice phys;
    VkDevice dev;
    uint32_t qfam;
    VkQueue queue;
    VkCommandPool cmd_pool;
    VkCommandBuffer cmd;
    VkFence fence;
    char dev_name[256];

    PFN_vkGetAccelerationStructureBuildSizesKHR pfnGetASBuildSizes;
    PFN_vkCreateAccelerationStructureKHR pfnCreateAS;
    PFN_vkDestroyAccelerationStructureKHR pfnDestroyAS;
    PFN_vkGetAccelerationStructureDeviceAddressKHR pfnGetASAddr;
    PFN_vkCmdBuildAccelerationStructuresKHR pfnCmdBuildAS;

    int gi_w;
    int gi_h;

    RTBuf vbo;
    RTBuf blas_buf;
    RTBuf tlas_buf;
    RTBuf scratch_buf;
    RTBuf inst_buf;
    RTBuf ubo_buf;
    RTBuf readback_buf;
    RTBuf upload_stage;

    VkAccelerationStructureKHR blas;
    VkAccelerationStructureKHR tlas;

    RTImg gi_img;
    RTImg rift_img;
    RTImg life_img;
    VkSampler lin_sampler;

    VkDescriptorSetLayout dsl;
    VkDescriptorPool dp;
    VkDescriptorSet ds;
    VkPipelineLayout pipe_layout;
    VkPipeline pipe;
    int frame_count;
    int faulted;
    int rift_dirty;
    int life_dirty;
};

static uint32_t find_mem_type(
    VkPhysicalDevice phys,
    uint32_t type_bits,
    VkMemoryPropertyFlags req
) {
    VkPhysicalDeviceMemoryProperties mp;
    vkGetPhysicalDeviceMemoryProperties(phys, &mp);
    for (uint32_t i = 0; i < mp.memoryTypeCount; ++i) {
        if ((type_bits & (1u << i)) && (mp.memoryTypes[i].propertyFlags & req) == req) {
            return i;
        }
    }
    return UINT32_MAX;
}

static int create_buf(
    VkRT5070 *rt,
    VkDeviceSize size,
    VkBufferUsageFlags usage,
    VkMemoryPropertyFlags mem_props,
    int need_addr,
    int map_it,
    RTBuf *out
) {
    memset(out, 0, sizeof(*out));
    out->size = size;

    VkBufferCreateInfo bci = {
        .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
        .size = size,
        .usage = usage | (need_addr ? VK_BUFFER_USAGE_SHADER_DEVICE_ADDRESS_BIT : 0),
        .sharingMode = VK_SHARING_MODE_EXCLUSIVE
    };
    if (vkCreateBuffer(rt->dev, &bci, NULL, &out->buf) != VK_SUCCESS) return -1;

    VkMemoryRequirements mr;
    vkGetBufferMemoryRequirements(rt->dev, out->buf, &mr);

    uint32_t mt = find_mem_type(rt->phys, mr.memoryTypeBits, mem_props);
    if (mt == UINT32_MAX) return -1;

    VkMemoryAllocateFlagsInfo mafi = {
        .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_FLAGS_INFO,
        .flags = need_addr ? VK_MEMORY_ALLOCATE_DEVICE_ADDRESS_BIT : 0
    };
    VkMemoryAllocateInfo mai = {
        .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
        .pNext = need_addr ? &mafi : NULL,
        .allocationSize = mr.size,
        .memoryTypeIndex = mt
    };
    if (vkAllocateMemory(rt->dev, &mai, NULL, &out->mem) != VK_SUCCESS) return -1;
    if (vkBindBufferMemory(rt->dev, out->buf, out->mem, 0) != VK_SUCCESS) return -1;

    if (map_it) {
        if (vkMapMemory(rt->dev, out->mem, 0, size, 0, &out->mapped) != VK_SUCCESS) return -1;
    }
    if (need_addr) {
        VkBufferDeviceAddressInfo bdai = {
            .sType = VK_STRUCTURE_TYPE_BUFFER_DEVICE_ADDRESS_INFO,
            .buffer = out->buf
        };
        out->addr = vkGetBufferDeviceAddress(rt->dev, &bdai);
    }
    return 0;
}

static void destroy_buf(VkRT5070 *rt, RTBuf *b) {
    if (!b->buf && !b->mem) return;
    if (b->mapped) vkUnmapMemory(rt->dev, b->mem);
    if (b->buf) vkDestroyBuffer(rt->dev, b->buf, NULL);
    if (b->mem) vkFreeMemory(rt->dev, b->mem, NULL);
    memset(b, 0, sizeof(*b));
}

static int create_img(
    VkRT5070 *rt,
    int w,
    int h,
    VkFormat fmt,
    VkImageUsageFlags usage,
    RTImg *out
) {
    memset(out, 0, sizeof(*out));
    out->w = w;
    out->h = h;
    out->fmt = fmt;
    out->layout = VK_IMAGE_LAYOUT_UNDEFINED;

    VkImageCreateInfo ici = {
        .sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO,
        .imageType = VK_IMAGE_TYPE_2D,
        .format = fmt,
        .extent = { (uint32_t)w, (uint32_t)h, 1 },
        .mipLevels = 1,
        .arrayLayers = 1,
        .samples = VK_SAMPLE_COUNT_1_BIT,
        .tiling = VK_IMAGE_TILING_OPTIMAL,
        .usage = usage,
        .sharingMode = VK_SHARING_MODE_EXCLUSIVE,
        .initialLayout = VK_IMAGE_LAYOUT_UNDEFINED
    };
    if (vkCreateImage(rt->dev, &ici, NULL, &out->img) != VK_SUCCESS) return -1;

    VkMemoryRequirements mr;
    vkGetImageMemoryRequirements(rt->dev, out->img, &mr);
    uint32_t mt = find_mem_type(rt->phys, mr.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
    if (mt == UINT32_MAX) return -1;

    VkMemoryAllocateInfo mai = {
        .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
        .allocationSize = mr.size,
        .memoryTypeIndex = mt
    };
    if (vkAllocateMemory(rt->dev, &mai, NULL, &out->mem) != VK_SUCCESS) return -1;
    if (vkBindImageMemory(rt->dev, out->img, out->mem, 0) != VK_SUCCESS) return -1;

    VkImageViewCreateInfo ivci = {
        .sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO,
        .image = out->img,
        .viewType = VK_IMAGE_VIEW_TYPE_2D,
        .format = fmt,
        .subresourceRange = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1 }
    };
    if (vkCreateImageView(rt->dev, &ivci, NULL, &out->view) != VK_SUCCESS) return -1;
    return 0;
}

static void destroy_img(VkRT5070 *rt, RTImg *im) {
    if (im->view) vkDestroyImageView(rt->dev, im->view, NULL);
    if (im->img) vkDestroyImage(rt->dev, im->img, NULL);
    if (im->mem) vkFreeMemory(rt->dev, im->mem, NULL);
    memset(im, 0, sizeof(*im));
}

static void cmd_img_barrier(
    VkCommandBuffer cmd,
    RTImg *im,
    VkImageLayout new_layout,
    VkAccessFlags src_acc,
    VkAccessFlags dst_acc,
    VkPipelineStageFlags src_stg,
    VkPipelineStageFlags dst_stg
) {
    VkImageMemoryBarrier b = {
        .sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,
        .srcAccessMask = src_acc,
        .dstAccessMask = dst_acc,
        .oldLayout = im->layout,
        .newLayout = new_layout,
        .srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
        .dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
        .image = im->img,
        .subresourceRange = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1 }
    };
    vkCmdPipelineBarrier(cmd, src_stg, dst_stg, 0, 0, NULL, 0, NULL, 1, &b);
    im->layout = new_layout;
}

/* Appends a vertical 3D wall segment from (ax, ay) to (bx, by) extruded [0 .. z_top] */
static int append_wall_quad(float *verts, int tri_count, float ax, float ay, float bx, float by, float z_top) {
    if (tri_count + 2 > MAX_TRIANGLES) return tri_count;
    float *v = verts + (size_t)tri_count * 9;
    /* Tri 1 */
    v[0] = ax; v[1] = ay; v[2] = 0.0f;
    v[3] = bx; v[4] = by; v[5] = 0.0f;
    v[6] = bx; v[7] = by; v[8] = z_top;
    /* Tri 2 */
    v[9]  = ax; v[10] = ay; v[11] = 0.0f;
    v[12] = bx; v[13] = by; v[14] = z_top;
    v[15] = ax; v[16] = ay; v[17] = z_top;
    return tri_count + 2;
}

/* Appends a slotted architectural bezel frame (with corner light-leak gaps for dramatic shafts!) */
static int append_slotted_frame(
    float *verts,
    int tri_count,
    float x0, float y0, float x1, float y1,
    float aspect,
    float z_top,
    float gap_frac
) {
    float X0 = x0 * aspect, X1 = x1 * aspect;
    float dx = (X1 - X0) * gap_frac;
    float dy = (y1 - y0) * gap_frac;
    /* Top & Bottom rails (slotted near corners & midpoint) */
    float xm = 0.5f * (X0 + X1);
    float ym = 0.5f * (y0 + y1);
    tri_count = append_wall_quad(verts, tri_count, X0 + dx, y0, xm - dx * 0.5f, y0, z_top);
    tri_count = append_wall_quad(verts, tri_count, xm + dx * 0.5f, y0, X1 - dx, y0, z_top);
    tri_count = append_wall_quad(verts, tri_count, X0 + dx, y1, xm - dx * 0.5f, y1, z_top);
    tri_count = append_wall_quad(verts, tri_count, xm + dx * 0.5f, y1, X1 - dx, y1, z_top);
    /* Left & Right rails */
    tri_count = append_wall_quad(verts, tri_count, X0, y0 + dy, X0, ym - dy * 0.5f, z_top);
    tri_count = append_wall_quad(verts, tri_count, X0, ym + dy * 0.5f, X0, y1 - dy, z_top);
    tri_count = append_wall_quad(verts, tri_count, X1, y0 + dy, X1, ym - dy * 0.5f, z_top);
    tri_count = append_wall_quad(verts, tri_count, X1, ym + dy * 0.5f, X1, y1 - dy, z_top);
    return tri_count;
}

static int build_scene_triangles(
    float *verts,
    float aspect,
    int dyn_count,
    const VkRTBoxWall *dyn_boxes
) {
    int tc = 0;
    /* 1. Architectural 3D bezels matching the 8VsvdCaSyaQ cyberpunk video panels!
     *    Slotted corners allow molten rift & cursor light to cast dramatic architectural shadow shafts! */
    const float cams[5][4] = {
        {0.021f, 0.025f, 0.172f, 0.176f},
        {0.224f, 0.025f, 0.375f, 0.176f},
        {0.419f, 0.025f, 0.570f, 0.176f},
        {0.629f, 0.025f, 0.780f, 0.176f},
        {0.829f, 0.025f, 0.980f, 0.176f},
    };
    for (int i = 0; i < 5; ++i) {
        tc = append_slotted_frame(verts, tc, cams[i][0], cams[i][1], cams[i][2], cams[i][3], aspect, 0.052f, 0.14f);
    }
    /* Central Binary Matrix Bezel (slotted louvers) */
    tc = append_slotted_frame(verts, tc, 0.246f, 0.239f, 0.755f, 0.750f, aspect, 0.046f, 0.12f);
    /* Left World Map + V9 Bezels */
    tc = append_slotted_frame(verts, tc, 0.023f, 0.549f, 0.224f, 0.749f, aspect, 0.050f, 0.14f);
    tc = append_slotted_frame(verts, tc, 0.023f, 0.236f, 0.112f, 0.382f, aspect, 0.048f, 0.15f);
    /* Right Password Cracker & Console Bezels */
    tc = append_slotted_frame(verts, tc, 0.838f, 0.237f, 0.980f, 0.446f, aspect, 0.050f, 0.14f);
    tc = append_slotted_frame(verts, tc, 0.872f, 0.460f, 0.980f, 0.750f, aspect, 0.050f, 0.14f);
    /* 5 Bottom PCB Circuit Frames */
    const float pcbs[5][2] = {
        {0.023f, 0.172f}, {0.224f, 0.374f}, {0.420f, 0.570f}, {0.615f, 0.765f}, {0.830f, 0.980f}
    };
    for (int i = 0; i < 5; ++i) {
        tc = append_slotted_frame(verts, tc, pcbs[i][0], 0.776f, pcbs[i][1], 0.928f, aspect, 0.048f, 0.15f);
    }

    /* 2. Dynamic live X11 Windows + Bad Sector 3D Monoliths */
    for (int i = 0; i < dyn_count; ++i) {
        const VkRTBoxWall *b = &dyn_boxes[i];
        tc = append_slotted_frame(verts, tc, b->x0, b->y0, b->x1, b->y1, aspect, b->z_top, 0.08f);
    }

    if (tc == 0) {
        /* Guarantee >= 1 offscreen dummy triangle so BLAS build is always non-empty */
        tc = append_wall_quad(verts, tc, -10.0f, -10.0f, -9.9f, -10.0f, 0.01f);
    }
    return tc;
}

static uint32_t *load_spv(const char *path, size_t *out_bytes) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); return NULL; }
    long len = ftell(f);
    if (len <= 0 || (len % 4) != 0) { fclose(f); return NULL; }
    rewind(f);
    uint32_t *buf = (uint32_t *)malloc((size_t)len);
    if (!buf) { fclose(f); return NULL; }
    if (fread(buf, 1, (size_t)len, f) != (size_t)len) {
        free(buf);
        fclose(f);
        return NULL;
    }
    fclose(f);
    *out_bytes = (size_t)len;
    return buf;
}

VkRT5070 *vk_rt5070_init(int gi_w, int gi_h, const char *comp_spv_path) {
    VkRT5070 *rt = (VkRT5070 *)calloc(1, sizeof(VkRT5070));
    if (!rt) return NULL;
    rt->gi_w = gi_w > 0 ? gi_w : 480;
    rt->gi_h = gi_h > 0 ? gi_h : 270;

    VkApplicationInfo app = {
        .sType = VK_STRUCTURE_TYPE_APPLICATION_INFO,
        .pApplicationName = "wallpaper-rt5070",
        .applicationVersion = VK_MAKE_VERSION(1, 0, 0),
        .apiVersion = VK_API_VERSION_1_3
    };
    VkInstanceCreateInfo ici = {
        .sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,
        .pApplicationInfo = &app
    };
    if (vkCreateInstance(&ici, NULL, &rt->instance) != VK_SUCCESS) {
        free(rt);
        return NULL;
    }

    uint32_t pcount = 0;
    vkEnumeratePhysicalDevices(rt->instance, &pcount, NULL);
    if (pcount == 0) {
        vkDestroyInstance(rt->instance, NULL);
        free(rt);
        return NULL;
    }
    VkPhysicalDevice *pdevs = (VkPhysicalDevice *)calloc(pcount, sizeof(VkPhysicalDevice));
    vkEnumeratePhysicalDevices(rt->instance, &pcount, pdevs);

    /* Prefer discrete NVIDIA GeForce RTX 5070 first! */
    VkPhysicalDevice chosen = VK_NULL_HANDLE;
    for (uint32_t i = 0; i < pcount; ++i) {
        VkPhysicalDeviceProperties pr;
        vkGetPhysicalDeviceProperties(pdevs[i], &pr);
        if (pr.deviceType == VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU && pr.vendorID == 0x10de) {
            chosen = pdevs[i];
            snprintf(rt->dev_name, sizeof(rt->dev_name), "%s", pr.deviceName);
            break;
        }
    }
    if (chosen == VK_NULL_HANDLE) {
        for (uint32_t i = 0; i < pcount; ++i) {
            VkPhysicalDeviceProperties pr;
            vkGetPhysicalDeviceProperties(pdevs[i], &pr);
            if (pr.deviceType == VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU) {
                chosen = pdevs[i];
                snprintf(rt->dev_name, sizeof(rt->dev_name), "%s", pr.deviceName);
                break;
            }
        }
    }
    if (chosen == VK_NULL_HANDLE) {
        VkPhysicalDeviceProperties pr;
        vkGetPhysicalDeviceProperties(pdevs[0], &pr);
        chosen = pdevs[0];
        snprintf(rt->dev_name, sizeof(rt->dev_name), "%s", pr.deviceName);
    }
    free(pdevs);
    rt->phys = chosen;

    /* Find compute + transfer queue family */
    uint32_t qcount = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(rt->phys, &qcount, NULL);
    VkQueueFamilyProperties *qprops = (VkQueueFamilyProperties *)calloc(qcount, sizeof(VkQueueFamilyProperties));
    vkGetPhysicalDeviceQueueFamilyProperties(rt->phys, &qcount, qprops);
    uint32_t qfam = UINT32_MAX;
    for (uint32_t i = 0; i < qcount; ++i) {
        if (qprops[i].queueFlags & VK_QUEUE_COMPUTE_BIT) {
            qfam = i;
            break;
        }
    }
    free(qprops);
    if (qfam == UINT32_MAX) {
        vkDestroyInstance(rt->instance, NULL);
        free(rt);
        return NULL;
    }
    rt->qfam = qfam;

    float qprio = 1.0f;
    VkDeviceQueueCreateInfo dqci = {
        .sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,
        .queueFamilyIndex = qfam,
        .queueCount = 1,
        .pQueuePriorities = &qprio
    };

    VkPhysicalDeviceRayQueryFeaturesKHR rq_feat = {
        .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_RAY_QUERY_FEATURES_KHR,
        .rayQuery = VK_TRUE
    };
    VkPhysicalDeviceAccelerationStructureFeaturesKHR as_feat = {
        .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ACCELERATION_STRUCTURE_FEATURES_KHR,
        .pNext = &rq_feat,
        .accelerationStructure = VK_TRUE
    };
    VkPhysicalDeviceVulkan12Features v12_feat = {
        .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES,
        .pNext = &as_feat,
        .bufferDeviceAddress = VK_TRUE
    };

    const char *dev_exts[] = {
        VK_KHR_ACCELERATION_STRUCTURE_EXTENSION_NAME,
        VK_KHR_RAY_QUERY_EXTENSION_NAME,
        VK_KHR_DEFERRED_HOST_OPERATIONS_EXTENSION_NAME
    };
    VkDeviceCreateInfo dci = {
        .sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
        .pNext = &v12_feat,
        .queueCreateInfoCount = 1,
        .pQueueCreateInfos = &dqci,
        .enabledExtensionCount = 3,
        .ppEnabledExtensionNames = dev_exts
    };
    if (vkCreateDevice(rt->phys, &dci, NULL, &rt->dev) != VK_SUCCESS) {
        fprintf(stderr, "vk_rt5070: vkCreateDevice failed on %s\n", rt->dev_name);
        vkDestroyInstance(rt->instance, NULL);
        free(rt);
        return NULL;
    }
    vkGetDeviceQueue(rt->dev, qfam, 0, &rt->queue);

    rt->pfnGetASBuildSizes = (PFN_vkGetAccelerationStructureBuildSizesKHR)
        vkGetDeviceProcAddr(rt->dev, "vkGetAccelerationStructureBuildSizesKHR");
    rt->pfnCreateAS = (PFN_vkCreateAccelerationStructureKHR)
        vkGetDeviceProcAddr(rt->dev, "vkCreateAccelerationStructureKHR");
    rt->pfnDestroyAS = (PFN_vkDestroyAccelerationStructureKHR)
        vkGetDeviceProcAddr(rt->dev, "vkDestroyAccelerationStructureKHR");
    rt->pfnGetASAddr = (PFN_vkGetAccelerationStructureDeviceAddressKHR)
        vkGetDeviceProcAddr(rt->dev, "vkGetAccelerationStructureDeviceAddressKHR");
    rt->pfnCmdBuildAS = (PFN_vkCmdBuildAccelerationStructuresKHR)
        vkGetDeviceProcAddr(rt->dev, "vkCmdBuildAccelerationStructuresKHR");

    if (!rt->pfnGetASBuildSizes || !rt->pfnCreateAS || !rt->pfnCmdBuildAS) {
        vk_rt5070_destroy(rt);
        return NULL;
    }

    VkCommandPoolCreateInfo cpci = {
        .sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
        .flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT,
        .queueFamilyIndex = qfam
    };
    vkCreateCommandPool(rt->dev, &cpci, NULL, &rt->cmd_pool);

    VkCommandBufferAllocateInfo cbai = {
        .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
        .commandPool = rt->cmd_pool,
        .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY,
        .commandBufferCount = 1
    };
    vkAllocateCommandBuffers(rt->dev, &cbai, &rt->cmd);

    VkFenceCreateInfo fci = { .sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO };
    vkCreateFence(rt->dev, &fci, NULL, &rt->fence);

    /* Allocate VBO & AS buffers */
    if (create_buf(
            rt, MAX_VERTICES * 3 * sizeof(float),
            VK_BUFFER_USAGE_ACCELERATION_STRUCTURE_BUILD_INPUT_READ_ONLY_BIT_KHR,
            VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
            1, 1, &rt->vbo
        ) < 0) {
        vk_rt5070_destroy(rt);
        return NULL;
    }

    VkAccelerationStructureGeometryTrianglesDataKHR tri_data = {
        .sType = VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_GEOMETRY_TRIANGLES_DATA_KHR,
        .vertexFormat = VK_FORMAT_R32G32B32_SFLOAT,
        .vertexData = { .deviceAddress = rt->vbo.addr },
        .vertexStride = 3 * sizeof(float),
        .maxVertex = MAX_VERTICES - 1,
        .indexType = VK_INDEX_TYPE_NONE_KHR
    };
    VkAccelerationStructureGeometryKHR blas_geom = {
        .sType = VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_GEOMETRY_KHR,
        .geometryType = VK_GEOMETRY_TYPE_TRIANGLES_KHR,
        .flags = VK_GEOMETRY_OPAQUE_BIT_KHR,
        .geometry = { .triangles = tri_data }
    };
    VkAccelerationStructureBuildGeometryInfoKHR blas_bgi = {
        .sType = VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_BUILD_GEOMETRY_INFO_KHR,
        .type = VK_ACCELERATION_STRUCTURE_TYPE_BOTTOM_LEVEL_KHR,
        .flags = VK_BUILD_ACCELERATION_STRUCTURE_PREFER_FAST_BUILD_BIT_KHR,
        .mode = VK_BUILD_ACCELERATION_STRUCTURE_MODE_BUILD_KHR,
        .geometryCount = 1,
        .pGeometries = &blas_geom
    };
    uint32_t max_prim = MAX_TRIANGLES;
    VkAccelerationStructureBuildSizesInfoKHR blas_sz = {
        .sType = VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_BUILD_SIZES_INFO_KHR
    };
    rt->pfnGetASBuildSizes(rt->dev, VK_ACCELERATION_STRUCTURE_BUILD_TYPE_DEVICE_KHR, &blas_bgi, &max_prim, &blas_sz);

    create_buf(
        rt, blas_sz.accelerationStructureSize,
        VK_BUFFER_USAGE_ACCELERATION_STRUCTURE_STORAGE_BIT_KHR,
        VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT, 1, 0, &rt->blas_buf
    );
    VkAccelerationStructureCreateInfoKHR blas_ci = {
        .sType = VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_CREATE_INFO_KHR,
        .buffer = rt->blas_buf.buf,
        .size = blas_sz.accelerationStructureSize,
        .type = VK_ACCELERATION_STRUCTURE_TYPE_BOTTOM_LEVEL_KHR
    };
    rt->pfnCreateAS(rt->dev, &blas_ci, NULL, &rt->blas);

    VkAccelerationStructureDeviceAddressInfoKHR blas_adai = {
        .sType = VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_DEVICE_ADDRESS_INFO_KHR,
        .accelerationStructure = rt->blas
    };
    VkDeviceAddress blas_addr = rt->pfnGetASAddr(rt->dev, &blas_adai);

    create_buf(
        rt, sizeof(VkAccelerationStructureInstanceKHR),
        VK_BUFFER_USAGE_ACCELERATION_STRUCTURE_BUILD_INPUT_READ_ONLY_BIT_KHR,
        VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
        1, 1, &rt->inst_buf
    );
    VkAccelerationStructureInstanceKHR *inst = (VkAccelerationStructureInstanceKHR *)rt->inst_buf.mapped;
    memset(inst, 0, sizeof(*inst));
    inst->transform.matrix[0][0] = 1.0f;
    inst->transform.matrix[1][1] = 1.0f;
    inst->transform.matrix[2][2] = 1.0f;
    inst->mask = 0xFF;
    inst->flags = VK_GEOMETRY_INSTANCE_TRIANGLE_FACING_CULL_DISABLE_BIT_KHR;
    inst->accelerationStructureReference = blas_addr;

    VkAccelerationStructureGeometryKHR tlas_geom = {
        .sType = VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_GEOMETRY_KHR,
        .geometryType = VK_GEOMETRY_TYPE_INSTANCES_KHR,
        .flags = VK_GEOMETRY_OPAQUE_BIT_KHR,
        .geometry = {
            .instances = {
                .sType = VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_GEOMETRY_INSTANCES_DATA_KHR,
                .arrayOfPointers = VK_FALSE,
                .data = { .deviceAddress = rt->inst_buf.addr }
            }
        }
    };
    VkAccelerationStructureBuildGeometryInfoKHR tlas_bgi = {
        .sType = VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_BUILD_GEOMETRY_INFO_KHR,
        .type = VK_ACCELERATION_STRUCTURE_TYPE_TOP_LEVEL_KHR,
        .flags = VK_BUILD_ACCELERATION_STRUCTURE_PREFER_FAST_TRACE_BIT_KHR,
        .mode = VK_BUILD_ACCELERATION_STRUCTURE_MODE_BUILD_KHR,
        .geometryCount = 1,
        .pGeometries = &tlas_geom
    };
    uint32_t inst_count = 1;
    VkAccelerationStructureBuildSizesInfoKHR tlas_sz = {
        .sType = VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_BUILD_SIZES_INFO_KHR
    };
    rt->pfnGetASBuildSizes(rt->dev, VK_ACCELERATION_STRUCTURE_BUILD_TYPE_DEVICE_KHR, &tlas_bgi, &inst_count, &tlas_sz);

    create_buf(
        rt, tlas_sz.accelerationStructureSize,
        VK_BUFFER_USAGE_ACCELERATION_STRUCTURE_STORAGE_BIT_KHR,
        VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT, 1, 0, &rt->tlas_buf
    );
    VkAccelerationStructureCreateInfoKHR tlas_ci = {
        .sType = VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_CREATE_INFO_KHR,
        .buffer = rt->tlas_buf.buf,
        .size = tlas_sz.accelerationStructureSize,
        .type = VK_ACCELERATION_STRUCTURE_TYPE_TOP_LEVEL_KHR
    };
    rt->pfnCreateAS(rt->dev, &tlas_ci, NULL, &rt->tlas);

    VkDeviceSize scratch_bytes = blas_sz.buildScratchSize > tlas_sz.buildScratchSize
        ? blas_sz.buildScratchSize : tlas_sz.buildScratchSize;
    if (scratch_bytes < 65536) scratch_bytes = 65536;
    create_buf(
        rt, scratch_bytes + 256,
        VK_BUFFER_USAGE_STORAGE_BUFFER_BIT,
        VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT, 1, 0, &rt->scratch_buf
    );

    create_buf(
        rt, sizeof(SceneUBOData),
        VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT,
        VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
        0, 1, &rt->ubo_buf
    );
    create_buf(
        rt, (VkDeviceSize)rt->gi_w * rt->gi_h * 8,
        VK_BUFFER_USAGE_TRANSFER_DST_BIT,
        VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_CACHED_BIT,
        0, 1, &rt->readback_buf
    );
    create_buf(
        rt, 640 * 360 * 4 * 2,
        VK_BUFFER_USAGE_TRANSFER_SRC_BIT,
        VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
        0, 1, &rt->upload_stage
    );

    /* Images */
    create_img(
        rt, rt->gi_w, rt->gi_h, VK_FORMAT_R16G16B16A16_SFLOAT,
        VK_IMAGE_USAGE_STORAGE_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT,
        &rt->gi_img
    );
    create_img(
        rt, 480, 270, VK_FORMAT_R8G8B8A8_UNORM,
        VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT,
        &rt->rift_img
    );
    create_img(
        rt, 640, 360, VK_FORMAT_R8G8B8A8_UNORM,
        VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT,
        &rt->life_img
    );

    VkSamplerCreateInfo sci = {
        .sType = VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO,
        .magFilter = VK_FILTER_LINEAR,
        .minFilter = VK_FILTER_LINEAR,
        .addressModeU = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,
        .addressModeV = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,
        .addressModeW = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE
    };
    vkCreateSampler(rt->dev, &sci, NULL, &rt->lin_sampler);

    /* Initialize image layouts cleanly */
    VkCommandBufferBeginInfo cbbi = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO };
    vkBeginCommandBuffer(rt->cmd, &cbbi);
    cmd_img_barrier(
        rt->cmd, &rt->gi_img, VK_IMAGE_LAYOUT_GENERAL,
        0, VK_ACCESS_TRANSFER_WRITE_BIT,
        VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT
    );
    VkClearColorValue zero_col = { {0.0f, 0.0f, 0.0f, 1.0f} };
    VkImageSubresourceRange sr = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1 };
    vkCmdClearColorImage(rt->cmd, rt->gi_img.img, VK_IMAGE_LAYOUT_GENERAL, &zero_col, 1, &sr);

    cmd_img_barrier(
        rt->cmd, &rt->rift_img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
        0, VK_ACCESS_TRANSFER_WRITE_BIT,
        VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT
    );
    vkCmdClearColorImage(rt->cmd, rt->rift_img.img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, &zero_col, 1, &sr);
    cmd_img_barrier(
        rt->cmd, &rt->rift_img, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
        VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT,
        VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT
    );

    cmd_img_barrier(
        rt->cmd, &rt->life_img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
        0, VK_ACCESS_TRANSFER_WRITE_BIT,
        VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT
    );
    vkCmdClearColorImage(rt->cmd, rt->life_img.img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, &zero_col, 1, &sr);
    cmd_img_barrier(
        rt->cmd, &rt->life_img, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
        VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT,
        VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT
    );
    vkEndCommandBuffer(rt->cmd);
    VkSubmitInfo si = {
        .sType = VK_STRUCTURE_TYPE_SUBMIT_INFO,
        .commandBufferCount = 1,
        .pCommandBuffers = &rt->cmd
    };
    vkResetFences(rt->dev, 1, &rt->fence);
    if (vkQueueSubmit(rt->queue, 1, &si, rt->fence) != VK_SUCCESS ||
        vkWaitForFences(rt->dev, 1, &rt->fence, VK_TRUE, VK_FENCE_TIMEOUT_NS) != VK_SUCCESS) {
        rt->faulted = 1;
        vk_rt5070_destroy(rt);
        return NULL;
    }

    /* Descriptor Set + Compute Pipeline */
    VkDescriptorSetLayoutBinding binds[5] = {
        { 0, VK_DESCRIPTOR_TYPE_ACCELERATION_STRUCTURE_KHR, 1, VK_SHADER_STAGE_COMPUTE_BIT, NULL },
        { 1, VK_DESCRIPTOR_TYPE_STORAGE_IMAGE,              1, VK_SHADER_STAGE_COMPUTE_BIT, NULL },
        { 2, VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,     1, VK_SHADER_STAGE_COMPUTE_BIT, NULL },
        { 3, VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,     1, VK_SHADER_STAGE_COMPUTE_BIT, NULL },
        { 4, VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER,             1, VK_SHADER_STAGE_COMPUTE_BIT, NULL },
    };
    VkDescriptorSetLayoutCreateInfo dslci = {
        .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,
        .bindingCount = 5,
        .pBindings = binds
    };
    vkCreateDescriptorSetLayout(rt->dev, &dslci, NULL, &rt->dsl);

    VkDescriptorPoolSize psizes[4] = {
        { VK_DESCRIPTOR_TYPE_ACCELERATION_STRUCTURE_KHR, 1 },
        { VK_DESCRIPTOR_TYPE_STORAGE_IMAGE,              1 },
        { VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,     2 },
        { VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER,             1 },
    };
    VkDescriptorPoolCreateInfo dpci = {
        .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO,
        .maxSets = 1,
        .poolSizeCount = 4,
        .pPoolSizes = psizes
    };
    vkCreateDescriptorPool(rt->dev, &dpci, NULL, &rt->dp);

    VkDescriptorSetAllocateInfo dsai = {
        .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,
        .descriptorPool = rt->dp,
        .descriptorSetCount = 1,
        .pSetLayouts = &rt->dsl
    };
    vkAllocateDescriptorSets(rt->dev, &dsai, &rt->ds);

    VkWriteDescriptorSetAccelerationStructureKHR w_as = {
        .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET_ACCELERATION_STRUCTURE_KHR,
        .accelerationStructureCount = 1,
        .pAccelerationStructures = &rt->tlas
    };
    VkDescriptorImageInfo di_gi = { VK_NULL_HANDLE, rt->gi_img.view, VK_IMAGE_LAYOUT_GENERAL };
    VkDescriptorImageInfo di_rift = { rt->lin_sampler, rt->rift_img.view, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL };
    VkDescriptorImageInfo di_life = { rt->lin_sampler, rt->life_img.view, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL };
    VkDescriptorBufferInfo db_ubo = { rt->ubo_buf.buf, 0, sizeof(SceneUBOData) };

    VkWriteDescriptorSet writes[5] = {
        { .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .pNext = &w_as, .dstSet = rt->ds, .dstBinding = 0, .descriptorCount = 1, .descriptorType = VK_DESCRIPTOR_TYPE_ACCELERATION_STRUCTURE_KHR },
        { .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstSet = rt->ds, .dstBinding = 1, .descriptorCount = 1, .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, .pImageInfo = &di_gi },
        { .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstSet = rt->ds, .dstBinding = 2, .descriptorCount = 1, .descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, .pImageInfo = &di_rift },
        { .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstSet = rt->ds, .dstBinding = 3, .descriptorCount = 1, .descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, .pImageInfo = &di_life },
        { .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstSet = rt->ds, .dstBinding = 4, .descriptorCount = 1, .descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, .pBufferInfo = &db_ubo },
    };
    vkUpdateDescriptorSets(rt->dev, 5, writes, 0, NULL);

    size_t spv_bytes = 0;
    uint32_t *spv = load_spv(comp_spv_path, &spv_bytes);
    if (!spv) {
        fprintf(stderr, "vk_rt5070: failed to load %s\n", comp_spv_path);
        vk_rt5070_destroy(rt);
        return NULL;
    }
    VkShaderModuleCreateInfo smci = {
        .sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO,
        .codeSize = spv_bytes,
        .pCode = spv
    };
    VkShaderModule sm = VK_NULL_HANDLE;
    vkCreateShaderModule(rt->dev, &smci, NULL, &sm);
    free(spv);

    VkPipelineLayoutCreateInfo plci = {
        .sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO,
        .setLayoutCount = 1,
        .pSetLayouts = &rt->dsl
    };
    vkCreatePipelineLayout(rt->dev, &plci, NULL, &rt->pipe_layout);

    VkComputePipelineCreateInfo cp_info = {
        .sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,
        .stage = {
            .sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,
            .stage = VK_SHADER_STAGE_COMPUTE_BIT,
            .module = sm,
            .pName = "main"
        },
        .layout = rt->pipe_layout
    };
    if (vkCreateComputePipelines(rt->dev, VK_NULL_HANDLE, 1, &cp_info, NULL, &rt->pipe) != VK_SUCCESS) {
        vkDestroyShaderModule(rt->dev, sm, NULL);
        vk_rt5070_destroy(rt);
        return NULL;
    }
    vkDestroyShaderModule(rt->dev, sm, NULL);
    return rt;
}

const char *vk_rt5070_device_name(const VkRT5070 *rt) {
    return rt ? rt->dev_name : "";
}

void vk_rt5070_upload_fields(
    VkRT5070 *rt,
    int rift_w, int rift_h, const void *rift_rgba,
    int life_w, int life_h, const void *life_rgba
) {
    if (!rt || rt->faulted || !rt->upload_stage.mapped) return;
    uint8_t *dst = (uint8_t *)rt->upload_stage.mapped;

    if (rift_rgba && rift_w == rt->rift_img.w && rift_h == rt->rift_img.h) {
        size_t rift_sz = (size_t)rift_w * rift_h * 4;
        if (RIFT_STAGE_OFFSET + rift_sz <= rt->upload_stage.size) {
            memcpy(dst + RIFT_STAGE_OFFSET, rift_rgba, rift_sz);
            rt->rift_dirty = 1;
        }
    }
    if (life_rgba && life_w == rt->life_img.w && life_h == rt->life_img.h) {
        size_t life_sz = (size_t)life_w * life_h * 4;
        if (LIFE_STAGE_OFFSET + life_sz <= rt->upload_stage.size) {
            memcpy(dst + LIFE_STAGE_OFFSET, life_rgba, life_sz);
            rt->life_dirty = 1;
        }
    }
}

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
) {
    if (!rt || rt->faulted || !out_rgba16f) return -1;

    /* 1. Rebuild dynamic 3D triangle mesh (architectural bezels + live windows + Bad Sectors) */
    int tri_count = build_scene_triangles((float *)rt->vbo.mapped, aspect, dynamic_box_count, dynamic_boxes);

    /* 2. Populate SceneUBO */
    SceneUBOData *u = (SceneUBOData *)rt->ubo_buf.mapped;
    memset(u, 0, sizeof(*u));
    if (emitter_count > 24) emitter_count = 24;
    if (emitter_count < 0) emitter_count = 0;
    u->meta[0] = time_sec;
    u->meta[1] = aspect;
    u->meta[2] = (float)emitter_count;
    u->meta[3] = (rt->frame_count == 0) ? 1.0f : temporal_blend;
    if (loads) memcpy(u->loads, loads, 4 * sizeof(float));
    if (mouse) memcpy(u->mouse, mouse, 4 * sizeof(float));
    if (emitter_count > 0 && emitters) {
        memcpy(u->emitters, emitters, (size_t)emitter_count * sizeof(VkRTEmitter));
    }

    VkDeviceAddress scratch_aligned = (rt->scratch_buf.addr + 255ULL) & ~255ULL;

    vkResetCommandBuffer(rt->cmd, 0);
    VkCommandBufferBeginInfo cbbi = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO };
    vkBeginCommandBuffer(rt->cmd, &cbbi);

    /* Batch staged rift & life field uploads into the same single command buffer */
    if (rt->rift_dirty) {
        cmd_img_barrier(
            rt->cmd, &rt->rift_img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
            VK_ACCESS_SHADER_READ_BIT, VK_ACCESS_TRANSFER_WRITE_BIT,
            VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT
        );
        VkBufferImageCopy bic_r = {
            .bufferOffset = RIFT_STAGE_OFFSET,
            .imageSubresource = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1 },
            .imageExtent = { (uint32_t)rt->rift_img.w, (uint32_t)rt->rift_img.h, 1 }
        };
        vkCmdCopyBufferToImage(rt->cmd, rt->upload_stage.buf, rt->rift_img.img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &bic_r);
        cmd_img_barrier(
            rt->cmd, &rt->rift_img, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
            VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT,
            VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT
        );
        rt->rift_dirty = 0;
    }

    if (rt->life_dirty) {
        cmd_img_barrier(
            rt->cmd, &rt->life_img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
            VK_ACCESS_SHADER_READ_BIT, VK_ACCESS_TRANSFER_WRITE_BIT,
            VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT
        );
        VkBufferImageCopy bic_l = {
            .bufferOffset = LIFE_STAGE_OFFSET,
            .imageSubresource = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1 },
            .imageExtent = { (uint32_t)rt->life_img.w, (uint32_t)rt->life_img.h, 1 }
        };
        vkCmdCopyBufferToImage(rt->cmd, rt->upload_stage.buf, rt->life_img.img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &bic_l);
        cmd_img_barrier(
            rt->cmd, &rt->life_img, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
            VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT,
            VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT
        );
        rt->life_dirty = 0;
    }

    /* Build BLAS on RTX 5070 RT Cores */
    VkAccelerationStructureGeometryTrianglesDataKHR tri_data = {
        .sType = VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_GEOMETRY_TRIANGLES_DATA_KHR,
        .vertexFormat = VK_FORMAT_R32G32B32_SFLOAT,
        .vertexData = { .deviceAddress = rt->vbo.addr },
        .vertexStride = 3 * sizeof(float),
        .maxVertex = (uint32_t)(tri_count * 3 - 1),
        .indexType = VK_INDEX_TYPE_NONE_KHR
    };
    VkAccelerationStructureGeometryKHR blas_geom = {
        .sType = VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_GEOMETRY_KHR,
        .geometryType = VK_GEOMETRY_TYPE_TRIANGLES_KHR,
        .flags = VK_GEOMETRY_OPAQUE_BIT_KHR,
        .geometry = { .triangles = tri_data }
    };
    VkAccelerationStructureBuildGeometryInfoKHR blas_bgi = {
        .sType = VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_BUILD_GEOMETRY_INFO_KHR,
        .type = VK_ACCELERATION_STRUCTURE_TYPE_BOTTOM_LEVEL_KHR,
        .flags = VK_BUILD_ACCELERATION_STRUCTURE_PREFER_FAST_BUILD_BIT_KHR,
        .mode = VK_BUILD_ACCELERATION_STRUCTURE_MODE_BUILD_KHR,
        .dstAccelerationStructure = rt->blas,
        .geometryCount = 1,
        .pGeometries = &blas_geom,
        .scratchData = { .deviceAddress = scratch_aligned }
    };
    VkAccelerationStructureBuildRangeInfoKHR blas_range = {
        .primitiveCount = (uint32_t)tri_count,
        .primitiveOffset = 0,
        .firstVertex = 0,
        .transformOffset = 0
    };
    const VkAccelerationStructureBuildRangeInfoKHR *p_blas_range = &blas_range;
    rt->pfnCmdBuildAS(rt->cmd, 1, &blas_bgi, &p_blas_range);

    VkMemoryBarrier mb_as = {
        .sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER,
        .srcAccessMask = VK_ACCESS_ACCELERATION_STRUCTURE_WRITE_BIT_KHR,
        .dstAccessMask = VK_ACCESS_ACCELERATION_STRUCTURE_READ_BIT_KHR | VK_ACCESS_ACCELERATION_STRUCTURE_WRITE_BIT_KHR
    };
    vkCmdPipelineBarrier(
        rt->cmd,
        VK_PIPELINE_STAGE_ACCELERATION_STRUCTURE_BUILD_BIT_KHR,
        VK_PIPELINE_STAGE_ACCELERATION_STRUCTURE_BUILD_BIT_KHR,
        0, 1, &mb_as, 0, NULL, 0, NULL
    );

    /* Build TLAS on RTX 5070 RT Cores */
    VkAccelerationStructureGeometryKHR tlas_geom = {
        .sType = VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_GEOMETRY_KHR,
        .geometryType = VK_GEOMETRY_TYPE_INSTANCES_KHR,
        .flags = VK_GEOMETRY_OPAQUE_BIT_KHR,
        .geometry = {
            .instances = {
                .sType = VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_GEOMETRY_INSTANCES_DATA_KHR,
                .arrayOfPointers = VK_FALSE,
                .data = { .deviceAddress = rt->inst_buf.addr }
            }
        }
    };
    VkAccelerationStructureBuildGeometryInfoKHR tlas_bgi = {
        .sType = VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_BUILD_GEOMETRY_INFO_KHR,
        .type = VK_ACCELERATION_STRUCTURE_TYPE_TOP_LEVEL_KHR,
        .flags = VK_BUILD_ACCELERATION_STRUCTURE_PREFER_FAST_TRACE_BIT_KHR,
        .mode = VK_BUILD_ACCELERATION_STRUCTURE_MODE_BUILD_KHR,
        .dstAccelerationStructure = rt->tlas,
        .geometryCount = 1,
        .pGeometries = &tlas_geom,
        .scratchData = { .deviceAddress = scratch_aligned }
    };
    VkAccelerationStructureBuildRangeInfoKHR tlas_range = { .primitiveCount = 1 };
    const VkAccelerationStructureBuildRangeInfoKHR *p_tlas_range = &tlas_range;
    rt->pfnCmdBuildAS(rt->cmd, 1, &tlas_bgi, &p_tlas_range);

    VkMemoryBarrier mb_trace = {
        .sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER,
        .srcAccessMask = VK_ACCESS_ACCELERATION_STRUCTURE_WRITE_BIT_KHR,
        .dstAccessMask = VK_ACCESS_ACCELERATION_STRUCTURE_READ_BIT_KHR | VK_ACCESS_SHADER_READ_BIT
    };
    vkCmdPipelineBarrier(
        rt->cmd,
        VK_PIPELINE_STAGE_ACCELERATION_STRUCTURE_BUILD_BIT_KHR,
        VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
        0, 1, &mb_trace, 0, NULL, 0, NULL
    );

    /* Dispatch Hardware RT-Core Compute Shader */
    vkCmdBindPipeline(rt->cmd, VK_PIPELINE_BIND_POINT_COMPUTE, rt->pipe);
    vkCmdBindDescriptorSets(rt->cmd, VK_PIPELINE_BIND_POINT_COMPUTE, rt->pipe_layout, 0, 1, &rt->ds, 0, NULL);
    vkCmdDispatch(rt->cmd, (uint32_t)((rt->gi_w + 7) / 8), (uint32_t)((rt->gi_h + 7) / 8), 1);

    /* Copy RGBA16F lightmap to host-cached buffer */
    cmd_img_barrier(
        rt->cmd, &rt->gi_img, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
        VK_ACCESS_SHADER_WRITE_BIT, VK_ACCESS_TRANSFER_READ_BIT,
        VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT
    );
    VkBufferImageCopy bic = {
        .bufferOffset = 0,
        .imageSubresource = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1 },
        .imageExtent = { (uint32_t)rt->gi_w, (uint32_t)rt->gi_h, 1 }
    };
    vkCmdCopyImageToBuffer(rt->cmd, rt->gi_img.img, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, rt->readback_buf.buf, 1, &bic);

    cmd_img_barrier(
        rt->cmd, &rt->gi_img, VK_IMAGE_LAYOUT_GENERAL,
        VK_ACCESS_TRANSFER_READ_BIT, VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT,
        VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT
    );

    vkEndCommandBuffer(rt->cmd);
    VkSubmitInfo si = { .sType = VK_STRUCTURE_TYPE_SUBMIT_INFO, .commandBufferCount = 1, .pCommandBuffers = &rt->cmd };
    vkResetFences(rt->dev, 1, &rt->fence);
    if (vkQueueSubmit(rt->queue, 1, &si, rt->fence) != VK_SUCCESS ||
        vkWaitForFences(rt->dev, 1, &rt->fence, VK_TRUE, VK_FENCE_TIMEOUT_NS) != VK_SUCCESS) {
        fprintf(stderr, "vk_rt5070: GPU fence timeout or device lost; falling back safely\n");
        rt->faulted = 1;
        return -1;
    }

    VkMappedMemoryRange inv = {
        .sType = VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE,
        .memory = rt->readback_buf.mem,
        .offset = 0,
        .size = VK_WHOLE_SIZE
    };
    vkInvalidateMappedMemoryRanges(rt->dev, 1, &inv);
    memcpy(out_rgba16f, rt->readback_buf.mapped, (size_t)rt->gi_w * rt->gi_h * 8);
    rt->frame_count++;
    return 0;
}

void vk_rt5070_destroy(VkRT5070 *rt) {
    if (!rt) return;
    if (rt->dev) {
        if (!rt->faulted) {
            vkDeviceWaitIdle(rt->dev);
        }
        if (rt->pipe) vkDestroyPipeline(rt->dev, rt->pipe, NULL);
        if (rt->pipe_layout) vkDestroyPipelineLayout(rt->dev, rt->pipe_layout, NULL);
        if (rt->dp) vkDestroyDescriptorPool(rt->dev, rt->dp, NULL);
        if (rt->dsl) vkDestroyDescriptorSetLayout(rt->dev, rt->dsl, NULL);
        if (rt->lin_sampler) vkDestroySampler(rt->dev, rt->lin_sampler, NULL);
        destroy_img(rt, &rt->gi_img);
        destroy_img(rt, &rt->rift_img);
        destroy_img(rt, &rt->life_img);
        if (rt->tlas && rt->pfnDestroyAS) rt->pfnDestroyAS(rt->dev, rt->tlas, NULL);
        if (rt->blas && rt->pfnDestroyAS) rt->pfnDestroyAS(rt->dev, rt->blas, NULL);
        destroy_buf(rt, &rt->vbo);
        destroy_buf(rt, &rt->blas_buf);
        destroy_buf(rt, &rt->tlas_buf);
        destroy_buf(rt, &rt->scratch_buf);
        destroy_buf(rt, &rt->inst_buf);
        destroy_buf(rt, &rt->ubo_buf);
        destroy_buf(rt, &rt->readback_buf);
        destroy_buf(rt, &rt->upload_stage);
        if (rt->fence) vkDestroyFence(rt->dev, rt->fence, NULL);
        if (rt->cmd_pool) vkDestroyCommandPool(rt->dev, rt->cmd_pool, NULL);
        vkDestroyDevice(rt->dev, NULL);
    }
    if (rt->instance) vkDestroyInstance(rt->instance, NULL);
    free(rt);
}
