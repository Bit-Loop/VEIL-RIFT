#version 330 core

in vec2 v_uv;
out vec4 frag_color;

uniform sampler2D u_video_tex;
uniform sampler2D u_rift_tex;
uniform sampler2D u_life_tex;
uniform sampler2D u_veil_tex;
uniform sampler2D u_hud_tex;
uniform sampler2D u_rt_gi_tex;

uniform vec2  u_res;
uniform vec2  u_life_grid;
uniform float u_time;
uniform vec4  u_loads; // x=cpu, y=gpu, z=ram, w=burst
uniform vec4  u_mouse; // x=mouse_uv_x, y=mouse_uv_y, z=mouse_speed_norm, w=aspect
uniform vec4  u_win[8];
uniform int   u_win_count;
uniform vec4  u_extra; // x=raytrace, y=dwell_sec, z=no_carrier_y, w=scar_remap_seed

const float PI = 3.14159265359;

// =============================================================================
// Hashes, Noise, Ridged Multifractal & Ordered 4x4 Bayer Dither
// =============================================================================
float hash11(float p) {
    p = fract(p * 0.1031);
    p *= p + 33.33;
    p *= p + p;
    return fract(p);
}

float hash21(vec2 p) {
    vec3 p3 = fract(vec3(p.xyx) * 0.1031);
    p3 += dot(p3, p3.yzx + 33.33);
    return fract((p3.x + p3.y) * p3.z);
}

float vnoise(vec2 p) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    vec2 u = f * f * (3.0 - 2.0 * f);
    return mix(
        mix(hash21(i), hash21(i + vec2(1.0, 0.0)), u.x),
        mix(hash21(i + vec2(0.0, 1.0)), hash21(i + vec2(1.0, 1.0)), u.x),
        u.y
    );
}

float ridge(vec2 p) {
    return 1.0 - abs(2.0 * vnoise(p) - 1.0);
}

float bayer4x4(vec2 px) {
    ivec2 p = ivec2(mod(px, 4.0));
    int idx = p.y * 4 + p.x;
    const float m[16] = float[16](
         0.0,  8.0,  2.0, 10.0,
        12.0,  4.0, 14.0,  6.0,
         3.0, 11.0,  1.0,  9.0,
        15.0,  7.0, 13.0,  5.0
    );
    return (m[idx] + 0.5) / 16.0 - 0.5;
}

float maxc(vec3 c) {
    return max(c.r, max(c.g, c.b));
}

// =============================================================================
// Opus Strict Dark-Red / Zero-Pink Color Algebra
// #000000 -> #070002 -> #6e020a -> #c40814 -> #cc3b00 -> #ff8f2e (G >> B, never pink)
// =============================================================================
vec3 bloodRamp(float e) {
    e = clamp(e, 0.0, 1.0);
    vec3 c = mix(vec3(0.0), vec3(0.027, 0.000, 0.008), smoothstep(0.00, 0.15, e)); // #070002
    c = mix(c, vec3(0.431, 0.008, 0.039), smoothstep(0.15, 0.45, e));              // #6e020a
    c = mix(c, vec3(0.769, 0.031, 0.078), smoothstep(0.45, 0.72, e));              // #c40814
    c = mix(c, vec3(0.800, 0.231, 0.000), smoothstep(0.72, 0.90, e));              // #cc3b00
    return mix(c, vec3(1.000, 0.560, 0.180), smoothstep(0.90, 1.00, e));           // #ff8f2e
}

vec3 noPink(vec3 c) {
    c = max(c, vec3(0.0));
    // Preserves rich royal ultraviolet (B >> R, e.g. #630ede) & emerald (R ~ 0),
    // while strictly crushing pink/magenta/salmon whenever R competes with B (R > 0.62*B & R > 0.34)!
    float rb_ratio = c.r / max(c.b, 0.001);
    float pink_gate = smoothstep(0.34, 0.60, c.r) * smoothstep(0.60, 0.98, rb_ratio);
    c.b -= max(c.b - c.g * 0.32, 0.0) * pink_gate * 0.96;
    // Also prevent royal ultraviolet from ever desaturating into pastel lilac
    if (c.b > 0.35 && c.r > 0.18 && rb_ratio < 0.65) {
        c.g = min(c.g, 0.12);
    }
    return max(c, vec3(0.0));
}

vec3 hueClip(vec3 c) {
    c = max(c, vec3(0.0));
    float m = max(c.r, max(c.g, c.b));
    return (m > 1.0) ? (c / m) : c;
}

vec3 sanitizeVeilAscii(vec3 v) {
    float mag = maxc(v);
    if (mag < 0.004) return vec3(0.0);
    if (v.b > 0.45 && v.g > 0.45 && v.r < 0.25) {
        return vec3(0.00, 0.80, 0.45) * mag;
    }
    if (v.b > 0.42 && v.r > 0.30) {
        return vec3(0.62, 0.025, 0.075) * mag;
    }
    return noPink(v);
}

vec3 ghostlyStrainColor(float strain) {
    float s = clamp(strain, 0.0, 1.0);
    if (s < 0.48) {
        float k = s / 0.48;
        return mix(vec3(0.00, 0.90, 0.20), vec3(0.00, 0.80, 0.45), k);
    }
    float k = (s - 0.48) / 0.52;
    if (k < 0.58) {
        return mix(vec3(0.431, 0.008, 0.039), vec3(0.769, 0.031, 0.078), k / 0.58);
    }
    return mix(vec3(0.769, 0.031, 0.078), vec3(0.820, 0.235, 0.008), (k - 0.58) / 0.42);
}

// =============================================================================
// Shared Shattered Rift Zones Math (matches corrupt-life-step.frag 1:1)
// =============================================================================
float evalRiftFractureJag(vec2 uv, vec2 px1080, float seed, float raw_t, float time_sec) {
    float env = 1.0 - smoothstep(5.4, 9.1, raw_t);
    vec2 warp = vec2(
        vnoise(uv * vec2(14.0, 8.5) + seed * 19.3) - 0.5,
        vnoise(uv * vec2(13.0, 9.5) - seed * 27.1) - 0.5
    ) * 0.055;
    vec2 uvw = uv + warp;
    // Asymmetric jagged cyber-lightning shards & coves (breaks up any smooth capsule contour!)
    float macro_shard = (ridge(uvw * vec2(19.0, 11.5) + seed * 31.0) - 0.56) * 0.24;
    float mid_fault   = (ridge(uvw * vec2(44.0, 27.0) - seed * 17.0 + floor(time_sec * 3.0) * 0.13) - 0.54) * 0.13;
    float byte_shear  = (hash21(vec2(floor(px1080.y / 8.0), floor(px1080.x / 22.0) + floor(seed * 73.0))) - 0.5) * 0.095;
    float scan_teeth  = (hash21(vec2(floor(px1080.y / 3.0), floor(time_sec * 8.0) + floor(seed * 97.0))) - 0.5) * 0.095;
    return (macro_shard + mid_fault + byte_shear + scan_teeth) * env;
}

struct Rift {
    float d;
    float age;
    float seed;
    float t;
    float tau;
    float dj;
    float z;
    float open;
    float moat;
    float seam;
    float relock;
    float arc;
};

Rift riftZones(vec4 r, float jag) {
    Rift o;
    o.d    = r.r;
    o.age  = r.g;
    o.seed = r.b;
    o.arc  = r.a;
    o.t    = (1.0 - o.age) * 10.0;

    float h  = smoothstep(2.6, 9.6, o.t);
    float h2 = h * h * (3.0 - 2.0 * h);
    o.tau = 0.10 + 0.90 * h2;

    float live = step(0.004, o.age) * step(0.004, o.d);
    float jag_env = 1.0 - smoothstep(8.2, 8.8, o.t);
    o.dj = o.d + jag * jag_env * smoothstep(0.02, 0.14, o.d);

    o.z = clamp((o.dj - o.tau) / max(1.0 - o.tau, 0.05), 0.0, 1.0) * live;
    o.open = step(o.tau, o.dj) * step(o.t, 8.65) * live;

    float moat_warm = 1.0 - smoothstep(8.5, 8.95, o.t);
    o.moat = smoothstep(o.tau - 0.20, o.tau, o.dj)
           * smoothstep(0.005, 0.085, o.d)
           * (1.0 - o.open)
           * moat_warm
           * live;

    float seam_win = smoothstep(8.2, 8.6, o.t) * (1.0 - smoothstep(9.75, 10.0, o.t));
    o.seam   = smoothstep(0.91, 0.975, o.d) * seam_win * live;
    o.relock = exp(-pow((o.t - 9.65) / 0.18, 2.0)) * smoothstep(0.88, 0.97, o.d) * live;
    return o;
}

// =============================================================================
// Shared Window-Aware ICE & 32x18 Bad Sector Territory Field
// =============================================================================
void evalWindowField(
    vec2 uv,
    float aspect,
    out float win_inside,
    out float win_halo,
    out float win_frame
) {
    win_inside = 0.0;
    win_halo   = 0.0;
    win_frame  = 0.0;
    for (int i = 0; i < 8; ++i) {
        if (i >= u_win_count) break;
        vec4 w = u_win[i];
        vec2 bmin = min(w.xy, w.zw);
        vec2 bmax = max(w.xy, w.zw);
        vec2 span = bmax - bmin;
        if (span.x < 0.015 || span.y < 0.015) continue;

        vec2 center  = 0.5 * (bmin + bmax);
        vec2 half_sz = 0.5 * span;
        vec2 d_asp   = (abs(uv - center) - half_sz) * vec2(aspect, 1.0);
        float sdist  = length(max(d_asp, 0.0)) + min(max(d_asp.x, d_asp.y), 0.0);

        win_inside = max(win_inside, 1.0 - smoothstep(-0.012, 0.002, sdist));
        win_halo   = max(win_halo, exp(-pow(max(sdist - 0.005, 0.0) / 0.036, 2.0)) * step(-0.006, sdist));
        win_frame  = max(win_frame, 1.0 - smoothstep(0.0008, 0.0024, abs(sdist)));
    }
}

float evalBadSectorMargin(vec2 uv, float t, float load_mix, float ram, float aspect, out float is_remap_scar) {
    vec2 grid = vec2(32.0, 18.0);
    vec2 block_id = floor(uv * grid);
    vec2 block_uv = (block_id + 0.5) / grid;

    float w_in_b, w_halo_b, w_frm_b;
    evalWindowField(block_uv, aspect, w_in_b, w_halo_b, w_frm_b);

    float n_blk  = vnoise(block_id * 0.28 + vec2(t * 0.021, -t * 0.016)) * 0.68
                 + vnoise(block_id * 0.63 - vec2(t * 0.031,  t * 0.024) + 11.7) * 0.32;
    float n_cont = vnoise(uv * grid * 0.28 + vec2(t * 0.021, -t * 0.016)) * 0.68
                 + vnoise(uv * grid * 0.63 - vec2(t * 0.031,  t * 0.024) + 11.7) * 0.32;

    float f_blk  = smoothstep(0.18, 0.82, n_blk);
    float f_cont = smoothstep(0.18, 0.82, n_cont);
    float F = mix(f_blk, f_cont, 0.24);

    float edge_prox = max(abs(block_uv.x - 0.5) * 2.0, abs(block_uv.y - 0.5) * 2.0);
    F += 0.14 * smoothstep(0.62, 0.98, edge_prox) * (1.0 - 0.55 * load_mix);
    if (block_uv.y < 0.14 && (block_uv.x < 0.32 || block_uv.x > 0.68)) {
        F -= 0.22 * (1.0 - load_mix);
    }
    F += 0.15 * (0.45 + 0.55 * load_mix) * w_halo_b - 0.22 * w_in_b * (1.0 - 0.62 * load_mix);

    is_remap_scar = 0.0;
    if (u_extra.w > 0.001) {
        float r_rem = hash21(block_id + floor(u_extra.w * 13.0) + 7.3);
        if (r_rem > 0.92 - 0.06 * ram) {
            float rem_strength = clamp(u_extra.w * 3.0, 0.0, 1.0);
            F += 0.32 * rem_strength;
            is_remap_scar = rem_strength;
        }
    }

    float cov = 0.08 + 0.78 * smoothstep(0.22, 0.88, load_mix);
    return F - (1.0 - cov);
}

// =============================================================================
// Video Signal & Layer Samplers (Layer 0, Layer 1 Gen 1B, Layer 2 Blood Abyss)
// =============================================================================
float rawVideoSig(vec2 uv) {
    vec2 c_uv = clamp(uv, vec2(0.001), vec2(0.999));
    vec3 c = texture(u_video_tex, c_uv).rgb;
    float sig = max(c.g, dot(c, vec3(0.28, 0.64, 0.08)));
    if (c_uv.y > 0.957) {
        sig *= 0.05;
    }
    return sig;
}

// LAYER 0: Dark-crushed hacker green + 11% hue-only "sick phosphor" chroma drift
vec3 sampleLayer0Green(vec2 uv, float aspect) {
    vec2 c_uv = clamp(uv, vec2(0.001), vec2(0.999));
    vec3 raw = texture(u_video_tex, c_uv).rgb;
    if (c_uv.y > 0.957) {
        raw *= 0.05;
    }
    float lum  = max(raw.g, dot(raw, vec3(0.25, 0.65, 0.10)));
    float gate = smoothstep(0.038, 0.165, lum);
    vec3 crushed = raw * mix(0.18, 1.05, gate);

    float base_y = dot(crushed, vec3(0.2126, 0.7152, 0.0722));
    if (base_y > 0.002) {
        vec2 centered = (c_uv - 0.5) * vec2(aspect, 1.0);
        float w_em = 0.5 + 0.5 * sin(centered.y * 6.8 - centered.x * 3.9 + u_time * 0.36);
        float w_am = 0.5 + 0.5 * cos(length(centered) * 8.4 - u_time * 0.52);
        vec3 sick_hue = mix(vec3(0.04, 0.92, 0.18), vec3(0.00, 0.80, 0.45), w_em);
        sick_hue = mix(sick_hue, vec3(0.58, 0.74, 0.06), w_am * 0.52);
        float sick_y = max(dot(sick_hue, vec3(0.2126, 0.7152, 0.0722)), 0.001);
        crushed = mix(crushed, sick_hue * (base_y / sick_y), 0.11);
    }
    return crushed;
}

// LAYER 1: Gen 1B High-Contrast Gloomy Multicolor Corruption
// >=45% pure #000000-#070002 voids! Phosphor/emerald + dark plum + crimson sync-tears + <=25% amber accent!
vec3 sampleLayer1Gen1B(
    vec2 uv,
    vec2 px,
    float surge,
    float cut_shove,
    float remap_boost,
    float draw_sector_grid,
    float dither,
    float aspect
) {
    float t = u_time;
    float cpu   = clamp(mix(u_loads.x, max(u_loads.x, surge), 0.58), 0.0, 1.0);
    float gpu   = clamp(mix(u_loads.y, max(u_loads.y, surge), 0.58), 0.0, 1.0);
    float ram   = clamp(mix(u_loads.z, max(u_loads.z, surge * 0.85), 0.55) + 0.35 * remap_boost, 0.0, 1.0);
    float burst = clamp(u_loads.w + 0.38 * surge, 0.0, 1.0);
    float activity = clamp(0.08 + 0.75 * surge + 0.36 * burst, 0.0, 1.25);

    float tick_coarse = floor(t * (2.6 + 8.4 * cpu + 6.5 * burst));
    float tick_fast   = floor(t * (8.5 + 19.0 * activity));

    // 1. Byte-aligned horizontal sync-tears + 0..0.15s fresh-cut scanline shove
    float macro_row = floor(uv.y * 24.0);
    float fine_row  = floor(uv.y * 96.0);
    float r_macro   = hash21(vec2(macro_row, tick_coarse));
    float r_fine    = hash21(vec2(fine_row, tick_fast));

    float tear_prob = clamp(0.06 + 0.30 * cpu + 0.20 * burst, 0.05, 0.52);
    float is_macro_torn = step(1.0 - tear_prob, r_macro);
    float macro_tear = is_macro_torn * (hash21(vec2(macro_row + 7.1, tick_coarse)) - 0.5);
    macro_tear = floor(macro_tear * 48.0) / 48.0 * (0.018 + 0.082 * cpu + 0.058 * burst);

    float is_fine_torn = step(1.0 - tear_prob * 0.65, r_fine);
    float fine_tear = is_fine_torn * (r_fine - 0.5) * (0.006 + 0.025 * cpu);

    float shove_shift = cut_shove * (hash11(fine_row + floor(t * 45.0)) - 0.5) * (14.0 / max(u_res.x, 640.0));

    float track_pos   = fract(t * (0.055 + 0.07 * gpu));
    float tracking    = exp(-pow((uv.y - track_pos) * 65.0, 2.0));
    float tape_jitter = tracking * sin(uv.y * 280.0 - t * 24.0) * (0.003 + 0.012 * activity);

    vec2 uv_dist = uv;
    uv_dist.x += macro_tear + fine_tear + tape_jitter + shove_shift;

    // 2. 32x18 RAM sector bit-rot / mosaic datamosh (NO DC pedestal -> pure #000000 black void preserved!)
    vec2 block_grid  = vec2(32.0, 18.0);
    vec2 block_id    = floor(uv_dist * block_grid);
    vec2 block_local = fract(uv_dist * block_grid);
    float block_seed = hash21(block_id + floor(t * (1.8 + 4.2 * ram)));
    float rot_prob   = clamp(0.08 + 0.24 * ram + 0.16 * burst + 0.45 * remap_boost, 0.06, 0.52);
    float rot_gate   = step(1.0 - rot_prob, block_seed);

    if (rot_gate > 0.5) {
        if (hash21(block_id * 3.17 + tick_coarse) < 0.52 || remap_boost > 0.2) {
            vec2 mosaic = vec2(160.0, 90.0);
            uv_dist = (floor(uv_dist * mosaic) + 0.5) / mosaic;
        } else {
            vec2 jump = (vec2(hash21(block_id + 1.3), hash21(block_id + 9.7)) - 0.5) * vec2(0.052, 0.032);
            uv_dist += jump * (0.42 + 0.72 * ram);
        }
    }

    // 3. Multi-tap chromatic phase-split + razor Sobel/cross edge detection
    float spread = 0.0024 + 0.0076 * gpu + 0.0068 * burst + abs(macro_tear) * 0.35;
    float v_slip = (0.0010 + 0.0044 * gpu) * sin(t * 2.9 + macro_row * 0.9);

    float s_main    = rawVideoSig(uv_dist);
    float s_red     = rawVideoSig(uv_dist + vec2( spread * 1.45, -v_slip * 0.5));
    float s_plum    = rawVideoSig(uv_dist + vec2(-spread * 1.35,  v_slip * 1.1));
    float s_emerald = rawVideoSig(uv_dist + vec2(-spread * 0.75, -v_slip * 0.85));

    vec2 texel = 1.0 / max(u_res, vec2(640.0, 360.0));
    float edge = clamp(
        (abs(s_main - rawVideoSig(uv_dist + vec2(texel.x * 1.5, 0.0)))
       + abs(s_main - rawVideoSig(uv_dist + vec2(0.0, texel.y * 1.5)))) * 3.0,
        0.0, 1.0
    );

    // 4. Aggressive #000000 black-crush so >=45-55% of pixels stay true pitch black,
    //    while keeping Gen 1B multicolor traces crisp & vivid inside both rifts and Bad Sectors!
    float crush_lo = mix(0.20, 0.18, draw_sector_grid);
    float crush_hi = mix(0.63, 0.59, draw_sector_grid);
    float crushed_main    = smoothstep(crush_lo,        crush_hi,        s_main    + dither * 0.065);
    float crushed_red     = smoothstep(crush_lo + 0.02, crush_hi + 0.02, s_red     + dither * 0.070);
    float crushed_plum    = smoothstep(crush_lo + 0.01, crush_hi + 0.01, s_plum    + dither * 0.070);
    float crushed_emerald = smoothstep(crush_lo + 0.02, crush_hi + 0.02, s_emerald + dither * 0.065);
    float gain = mix(0.95, 1.02, draw_sector_grid);
    float core = clamp((crushed_main * 1.06 + edge * 0.70) * gain, 0.0, 1.28);

    // 5. True Multicolor Gen 1B Palette (Phosphor, Emerald, Royal Ultraviolet, Crimson & Burnt Amber!)
    const vec3 COL_PHOSPH  = vec3(0.000, 0.935, 0.170);
    const vec3 COL_EMERALD = vec3(0.000, 0.830, 0.470);
    const vec3 COL_CRIMSON = vec3(0.780, 0.031, 0.078); // #c70814
    const vec3 COL_OXBLOOD = vec3(0.470, 0.000, 0.070); // deep oxblood
    const vec3 COL_PLUM    = vec3(0.390, 0.055, 0.875); // vivid royal ultraviolet (B >> R, zero pink!)
    const vec3 COL_AMBER   = vec3(0.860, 0.320, 0.015); // burnt sulfur accent (<=22% of tiles)

    vec2 centered = (uv - 0.5) * vec2(aspect, 1.0);
    float rad = length(centered);
    float ang = atan(centered.y, centered.x);

    float w_plum  = 0.5 + 0.5 * sin(centered.x * 5.2 - centered.y * 3.8 - t * 0.45 + sin(ang * 3.0 - t * 0.28));
    float w_blood = 0.5 + 0.5 * sin(centered.x * 3.6 + centered.y * 6.0 + t * 0.58 + macro_tear * 16.0);
    float w_amber = 0.5 + 0.5 * cos(rad * 9.0 - t * 0.65 + ang * 2.0);
    float w_em    = 0.5 + 0.5 * sin(centered.y * 7.4 - centered.x * 4.3 + t * 0.40);

    // Partitioned color veins so green/emerald & royal ultraviolet stay vivid alongside crimson & amber!
    vec3 trace = mix(COL_PHOSPH, COL_EMERALD, smoothstep(0.40, 0.80, w_em) * 0.80);
    float plum_zone  = smoothstep(0.46, 0.80, w_plum)  * (0.64 + 0.32 * gpu);
    float blood_zone = smoothstep(0.64, 0.90, w_blood) * (0.28 + 0.46 * surge);
    float amber_zone = smoothstep(0.74, 0.95, w_amber) * (0.24 + 0.34 * surge) * smoothstep(0.40, 0.92, core);

    trace = mix(trace, COL_PLUM,    clamp(plum_zone, 0.0, 0.88));
    trace = mix(trace, COL_CRIMSON, clamp(blood_zone, 0.0, 0.66));
    trace = mix(trace, COL_AMBER,   clamp(amber_zone, 0.0, 0.56));

    // Strictly multiplied by core -> zero signal == #000000 pitch black!
    vec3 col = trace * core;

    vec3 f_red  = COL_CRIMSON * max(0.0, crushed_red     - crushed_main * 0.58) * (0.95 + 0.50 * cpu);
    vec3 f_plum = COL_PLUM    * max(0.0, crushed_plum    - crushed_main * 0.45) * (1.00 + 0.55 * gpu);
    vec3 f_em   = COL_EMERALD * max(0.0, crushed_emerald - crushed_main * 0.56) * (0.88 + 0.50 * ram);
    col = max(col, max(f_red, max(f_plum, f_em)));

    // Sync-torn scan-bands: recolor existing traces ONLY (no DC pedestal on black gaps!)
    if (is_macro_torn > 0.5) {
        vec3 tear_col = (hash11(macro_row + tick_coarse) < 0.55) ? COL_CRIMSON : COL_OXBLOOD;
        float tear_sig = max(core, crushed_red * gain);
        col = mix(col, tear_col * (1.08 * tear_sig), 0.66);
        float wire = step(0.95, fract(uv.y * 24.0)) * step(0.0, dither);
        col = max(col, tear_col * wire * 0.28);
    }

    // Corrupted RAM sector: recolors traces ONLY + crisp 1px Bayer-stippled sector border!
    if (rot_gate > 0.5) {
        float pick = hash11(block_seed * 91.7);
        vec3 rot_col = (pick < 0.42) ? COL_PLUM
                     : ((pick < 0.64) ? COL_EMERALD
                     : ((pick < 0.82) ? COL_OXBLOOD : COL_AMBER));
        if (remap_boost > 0.2) rot_col = COL_AMBER;
        float bdr = step(block_local.x, 0.028) + step(0.972, block_local.x)
                  + step(block_local.y, 0.048) + step(0.952, block_local.y);
        float bdr_stipple = step(0.0, dither) * clamp(bdr, 0.0, 1.0);
        col = mix(col, rot_col * (core * 1.15), 0.78);
        col = max(col, rot_col * bdr_stipple * (0.32 + 0.28 * ram));
    } else if (draw_sector_grid > 0.5) {
        vec2 raw_local = fract(uv * block_grid);
        float sbdr = step(raw_local.x, 0.022) + step(0.978, raw_local.x)
                   + step(raw_local.y, 0.038) + step(0.962, raw_local.y);
        float sbdr_stipple = step(0.18, dither + 0.5) * clamp(sbdr, 0.0, 1.0);
        vec3 grid_tint = (hash21(floor(uv * block_grid)) < 0.55) ? COL_OXBLOOD : COL_EMERALD * 0.55;
        col = max(col, grid_tint * sbdr_stipple * 0.38);
    }

    col = max(col, mix(COL_PLUM, COL_EMERALD, 0.55) * tracking * (0.02 + 0.24 * edge));

    // Living non-demonic glitch ASCII ([tDd//WATCHDOG] & [ICE//2400]) in dark gaps
    float dark_gap = 1.0 - smoothstep(0.08, 0.38, core);
    vec4 veil_px = texture(u_veil_tex, clamp(uv_dist, vec2(0.001), vec2(0.999)));
    vec3 veil_clean = sanitizeVeilAscii(veil_px.rgb) * (0.48 + 0.52 * dark_gap) * 0.85;
    col = max(col, veil_clean);

    // Subtle #070002 stippled floor ONLY where draw_sector_grid < 0.5, keeping >=45% pixels at #000-#070002!
    float scan = 0.82 + 0.18 * cos(px.y * PI);
    vec3 abyss_floor = vec3(0.027, 0.000, 0.008) * step(0.10, dither) * (1.0 - 0.5 * draw_sector_grid);
    col = max(col, abyss_floor * dark_gap);

    return hueClip(noPink(col * scan));
}

// =============================================================================
// LAYER 2: Dark Blackwall Blood Chasm (#070002-#6e020a) + Thin 2-4px Jagged Seam & Razor Filaments
// =============================================================================
float evalSpineEmissive(
    vec2 px,
    float d_raw,
    float z_depth,
    float rift_t,
    float seed,
    float arc,
    float heartbeat,
    float gpu,
    out float out_core_chasm
) {
    float tq_fast = floor(u_time * 12.0);
    float tq_slow = floor(u_time * 3.0);
    float tq = (rift_t < 2.6) ? tq_fast : mix(tq_fast, tq_slow, smoothstep(2.6, 5.2, rift_t));

    // Domain-warped razor-thin branching Gen 3 lightning filaments
    vec2 warp = vec2(
        vnoise(px / 20.0 + vec2(tq * 0.37, seed * 19.0)) - 0.5,
        vnoise(px / 20.0 - vec2(seed * 13.0, tq * 0.41)) - 0.5
    ) * 6.0;
    vec2 q = (px + warp * (1.0 + 0.55 * gpu)) / 5.5 + hash11(tq + seed * 31.7) * 7.0;

    float r1 = ridge(q * 0.35);
    float r2 = ridge(q * (0.90 + 0.32 * gpu) + 3.1);
    float r3 = ridge(q * (1.85 + 0.90 * gpu) - 5.3);

    float fil_core   = pow(clamp(r1 * r2, 0.0, 1.0), 6.2);
    float fil_branch = pow(clamp(r2 * r3, 0.0, 1.0), 7.2) * (0.42 + 0.90 * gpu);
    float fil = clamp(fil_core * 1.25 + fil_branch, 0.0, 1.35);

    float coag_fil  = 1.0 - smoothstep(5.2, 8.3, rift_t);
    float coag_seam = 1.0 - 0.48 * smoothstep(5.0, 8.4, rift_t);

    // Opus #12 Item 1: Dark chasm + thin 2-4px jagged molten seam (exp(-|dw - 0.965| * 90))
    out_core_chasm = smoothstep(0.74, 0.92, d_raw);
    float dw = d_raw
             + 0.058 * (vnoise(px / 7.0 + tq * 0.40 + seed * 17.0) - 0.5)
             + 0.020 * (hash11(floor(px.y / 2.0) + tq) - 0.5);
    float thin_seam = exp(-abs(dw - 0.962) * 85.0) * step(0.66, d_raw);

    float cut_flash = (1.0 - smoothstep(0.0, 0.15, rift_t)) * step(0.001, arc);
    float e_raw = thin_seam * (0.58 + 0.42 * arc + 0.22 * cut_flash) * coag_seam
                + fil * smoothstep(0.18, 0.78, z_depth) * (0.46 + 0.54 * arc) * (0.55 + 0.50 * gpu) * coag_fil
                + heartbeat * 0.18 * thin_seam * coag_seam;

    // Cap e at 0.86 once arc cools so only fresh arcs reach hot ember (#ff8f2e)
    float e_cap = mix(0.86, 1.00, clamp(arc, 0.0, 1.0));
    return min(e_cap, e_raw);
}

vec3 sampleLayer2Abyss(
    vec2 uv,
    vec2 px,
    float d_raw,
    float z_depth,
    float rift_t,
    float seed,
    float arc,
    float heartbeat,
    float gpu,
    bool dark_invert_chasm
) {
    float core_chasm;
    float e = evalSpineEmissive(px, d_raw, z_depth, rift_t, seed, arc, heartbeat, gpu, core_chasm);

    // Deep #000000 -> #070002 -> #6e020a arterial abyssal chasm (dark contrast inversion!)
    float coag = 1.0 - 0.50 * smoothstep(5.0, 8.4, rift_t);
    float ghost_sig = smoothstep(0.28, 0.78, rawVideoSig(uv + vec2(0.004 * sin(uv.y * 40.0 + u_time), 0.0)));
    float chasm_floor = dark_invert_chasm
        ? (0.04 + 0.12 * heartbeat * core_chasm + 0.08 * ghost_sig * coag)
        : (0.14 * core_chasm + 0.20 * heartbeat * core_chasm + 0.08 * ghost_sig * coag);

    vec3 abyss = bloodRamp(max(e, chasm_floor));

    // Subtle oxblood WATCHDOG/ICE ASCII bleed inside dark Blackwall chasm
    vec4 veil_px = texture(u_veil_tex, clamp(uv, vec2(0.001), vec2(0.999)));
    float v_lum = maxc(veil_px.rgb);
    abyss = max(abyss, bloodRamp(0.28 + 0.30 * v_lum) * step(0.10, v_lum) * 0.65);

    float scan = 0.84 + 0.16 * cos(px.y * PI);
    return hueClip(noPink(abyss * scan));
}

float ggxD(float NdotH, float roughness) {
    float a  = roughness * roughness;
    float a2 = a * a;
    float d  = NdotH * NdotH * (a2 - 1.0) + 1.0;
    return a2 / max(PI * d * d, 0.0001);
}

void main() {
    vec2 uv = v_uv;
    vec2 px = uv * u_res;
    float aspect = max(u_mouse.w, 0.5);
    float dither  = bayer4x4(px);
    float bayer01 = dither + 0.5;

    float cpu   = u_loads.x;
    float gpu   = u_loads.y;
    float ram   = u_loads.z;
    float burst = u_loads.w;
    float load_mix = clamp(0.42 * cpu + 0.36 * gpu + 0.22 * ram, 0.0, 1.0);
    bool rt_enabled = (u_extra.x > 0.5);

    // Arterial lub-dub physiological heartbeat (50 + 90 * cpu BPM)
    float bpm = 50.0 + 90.0 * cpu;
    float beat_p = fract(u_time * (bpm / 60.0));
    float heartbeat = clamp(
        exp(-pow((beat_p - 0.08) / 0.045, 2.0)) + 0.68 * exp(-pow((beat_p - 0.23) / 0.055, 2.0)),
        0.0, 1.0
    );

    // =========================================================================
    // 1. GHOSTLY GPU GAME OF LIFE MULTI-TAP AURA & REFRACTION FIELD
    // =========================================================================
    vec2 l_texel = 1.0 / max(u_life_grid, vec2(640.0, 360.0));
    vec4 l_c  = texture(u_life_tex, uv);
    vec4 l_n  = texture(u_life_tex, uv + vec2(0.0, -l_texel.y));
    vec4 l_s  = texture(u_life_tex, uv + vec2(0.0,  l_texel.y));
    vec4 l_e  = texture(u_life_tex, uv + vec2( l_texel.x, 0.0));
    vec4 l_w  = texture(u_life_tex, uv + vec2(-l_texel.x, 0.0));
    vec4 l_ne = texture(u_life_tex, uv + vec2( l_texel.x, -l_texel.y));
    vec4 l_sw = texture(u_life_tex, uv + vec2(-l_texel.x,  l_texel.y));

    float life_alive_blur = l_c.r * 0.34 + (l_n.r + l_s.r + l_e.r + l_w.r) * 0.12 + (l_ne.r + l_sw.r) * 0.09;
    float life_ghost_blur = l_c.g * 0.36 + (l_n.g + l_s.g + l_e.g + l_w.g) * 0.13 + (l_ne.g + l_sw.g) * 0.06;
    float life_energy = clamp(life_alive_blur * 0.85 + life_ghost_blur * 0.55, 0.0, 1.0);
    vec2  life_grad   = vec2(l_e.g - l_w.g, l_s.g - l_n.g);

    // =========================================================================
    // 2. WINDOW-AWARE ICE & FRAYED 32x18 BAD SECTORS
    // =========================================================================
    float win_inside, win_halo, win_frame;
    evalWindowField(uv, aspect, win_inside, win_halo, win_frame);

    float scan_band_3px = floor(px.y / 3.0);
    float scan_band_2px = floor(px.y / 2.0);
    float tear_tooth = (hash21(vec2(scan_band_3px, floor(u_time * 8.0))) - 0.5) * 0.34
                     + (hash21(vec2(scan_band_2px, floor(uv.x * 48.0))) - 0.5) * 0.22
                     + (vnoise(uv * vec2(42.0, 24.0) + u_time * 0.35) - 0.5) * 0.28;

    float remap_scar = 0.0;
    float sec_margin = evalBadSectorMargin(uv, u_time, load_mix, ram, aspect, remap_scar);
    float sec_perturbed = sec_margin + (life_energy - 0.28) * 0.14 + dither * 0.075 + tear_tooth * 0.045;
    float is_bad_sector = step(0.0, sec_perturbed);
    float sec_rim = (1.0 - is_bad_sector) * smoothstep(-0.07, 0.0, sec_margin + tear_tooth * 0.025);

    // =========================================================================
    // 3. SHATTERED CYBER-LIGHTNING RIFT ZONES + GRADIENT FOR 3D CANYON & RT
    // =========================================================================
    vec4 rift_raw = texture(u_rift_tex, uv);
    float raw_t = (1.0 - rift_raw.g) * 10.0;

    // Asymmetric tectonic lightning shards + scanline shear + 0.28*(life_energy - 0.35) cellular bite!
    float frac_jag = evalRiftFractureJag(uv, px, rift_raw.b, raw_t, u_time);
    float jag = 0.28 * (life_energy - 0.35) + frac_jag + 0.05 * dither;
    Rift rz = riftZones(rift_raw, jag);

    // Compute spatial gradient of rift distance field d (points inward toward nearest spine!)
    vec2 g_eps = vec2(3.5) / max(u_res, vec2(640.0, 360.0));
    float rd_e = texture(u_rift_tex, clamp(uv + vec2( g_eps.x, 0.0), vec2(0.001), vec2(0.999))).r;
    float rd_w = texture(u_rift_tex, clamp(uv + vec2(-g_eps.x, 0.0), vec2(0.001), vec2(0.999))).r;
    float rd_s = texture(u_rift_tex, clamp(uv + vec2(0.0,  g_eps.y), vec2(0.001), vec2(0.999))).r;
    float rd_n = texture(u_rift_tex, clamp(uv + vec2(0.0, -g_eps.y), vec2(0.001), vec2(0.999))).r;
    vec2 grad_d = vec2(rd_e - rd_w, rd_s - rd_n);
    float grad_len = length(grad_d);
    vec2 dir_to_spine = (grad_len > 0.0012) ? (grad_d / grad_len) : vec2(0.0);

    vec2 cam_lean = (u_mouse.xy - 0.5) * 0.18;
    vec3 view_dir = normalize(vec3((uv - 0.5) * vec2(aspect, 1.0) - cam_lean, -1.35));

    vec2 uv_L0 = uv + life_grad * 0.0045;
    vec2 uv_L1 = uv + life_grad * 0.0120;
    vec2 uv_L2 = uv + life_grad * 0.0085;

    if (rt_enabled) {
        // Opus #12 Item 2: Recessed floor parallax tucking inner layers underneath the glass lip!
        vec2 eye_para = (view_dir.xy / max(-view_dir.z, 0.6)) * 0.010;
        vec2 canyon_para = (rz.open > 0.5) ? (-dir_to_spine * (1.0 - rz.z) * 0.013) : vec2(0.0);
        uv_L1 += canyon_para + eye_para * 0.45;
        uv_L2 += canyon_para * 1.35 + eye_para * 1.00;
    }

    // =========================================================================
    // 4. EVALUATE BASE DESKTOP (LAYER 0 GREEN vs 100%-STRENGTH LAYER 1 BAD SECTORS)
    // =========================================================================
    vec3 base_col = sampleLayer0Green(uv_L0, aspect);

    if (sec_rim > 0.01) {
        base_col *= (1.0 - 0.38 * sec_rim);
        base_col = max(base_col, vec3(0.431, 0.008, 0.039) * sec_rim * step(0.0, dither + 0.12) * 0.52);
    }

    if (is_bad_sector > 0.5) {
        // Full 100%-strength Gen 1B with crisp traces & 1px stippled byte-sector border!
        float outer_surge = clamp(0.22 + 0.76 * load_mix + 0.35 * burst, 0.20, 1.0);
        base_col = sampleLayer1Gen1B(uv_L1, px, outer_surge, 0.0, remap_scar, 1.0, dither, aspect);

        // Subtle 3D beveled step-down shadow around recessed Bad Sector edges in RT mode
        if (rt_enabled && sec_margin < 0.075) {
            float sm_e, sm_n, rem_tmp;
            sm_e = evalBadSectorMargin(uv + vec2(g_eps.x, 0.0), u_time, load_mix, ram, aspect, rem_tmp);
            sm_n = evalBadSectorMargin(uv - vec2(0.0, g_eps.y), u_time, load_mix, ram, aspect, rem_tmp);
            vec2 sec_inward = normalize(vec2(sm_e - sec_margin, sec_margin - sm_n) + vec2(0.0001));
            float sec_bevel = dot(sec_inward, normalize(vec2(0.55, -0.75)));
            float edge_w = 1.0 - smoothstep(0.0, 0.075, sec_margin);
            base_col *= mix(1.0, 0.42, clamp(-sec_bevel, 0.0, 1.0) * edge_w);
            base_col = max(base_col, bloodRamp(0.45) * clamp(sec_bevel, 0.0, 1.0) * edge_w * 0.55);
        }
    }

    // Dark Moat outside the rift lip: crushes BOTH Layer 0 Green AND Layer 1 Bad Sectors!
    if (rz.moat > 0.002) {
        base_col *= (1.0 - 0.78 * rz.moat);
        float eat = 0.82 * life_alive_blur * smoothstep(0.02, 0.22, rz.moat);
        base_col *= (1.0 - eat);
        float cell_rim = clamp(life_alive_blur * (1.2 - life_alive_blur) * 2.6, 0.0, 1.0) * smoothstep(0.02, 0.20, rz.moat);
        base_col = max(base_col, vec3(0.431, 0.008, 0.039) * cell_rim * 0.86);
    }

    // =========================================================================
    // 5. PIXEL-SELECTED RIFT DEPTH COMPOSITING (1 LAYER DEEPER, ZERO CROSSFADE!)
    // =========================================================================
    vec3 col = base_col;

    if (rz.open > 0.5) {
        float lip_sel = (rz.z < 0.10) ? step(bayer01, rz.z / 0.10 + 0.60 * life_alive_blur) : 1.0;

        if (lip_sel > 0.5) {
            float cut_shove = 1.0 - smoothstep(0.0, 0.15, rz.t);
            vec2 lip_jitter = vec2(
                (1.0 - smoothstep(0.0, 0.14, rz.z)) * (hash11(scan_band_2px + floor(u_time * 18.0)) - 0.5) * (0.018 + 0.020 * rz.arc),
                0.0
            );

            vec3 inner_col;
            if (is_bad_sector > 0.5) {
                // Opus #12 Item 4: INVERT CONTRAST over Layer 1 Bad Sectors!
                // Cuts a pitch-dark Blackwall Abyss chasm (mean luma x0.4, thin seam & razor filaments only)
                // into the bright multicolor corrupted field!
                inner_col = sampleLayer2Abyss(
                    uv_L2 + lip_jitter, px, rz.d, max(rz.z, 0.38),
                    rz.t, rz.seed, rz.arc, heartbeat, gpu, true
                );
            } else {
                // Rift over Layer 0 Green -> Wide Vivid Gen 1B Multicolor Mid-Shelf -> Dark-Reddish Chasm + Thin Seam
                float age_calm  = mix(1.0, 0.48, smoothstep(2.6, 6.8, rz.t));
                float mid_surge = mix(0.28, 0.94, smoothstep(0.06, 0.52, rz.z)) * age_calm;

                vec3 g1b = sampleLayer1Gen1B(uv_L1 + lip_jitter, px, mid_surge, cut_shove, 0.0, 0.0, dither, aspect);

                // Smooth black-crushing Red-Shift over z = 0.56..0.86 -> transitions Gen 1B into dark Gen 3 core!
                float rs = smoothstep(0.56, 0.86, rz.z);
                vec3 g1b_blood = bloodRamp(smoothstep(0.24, 0.88, maxc(g1b)) * 0.86);
                g1b = mix(g1b, g1b_blood, rs);

                // Carve dark #070002-#6e020a chasm in spine core (z > 0.66) so the thin 2-4px molten seam pops!
                float chasm_carve = smoothstep(0.66, 0.88, rz.z);
                g1b *= (1.0 - 0.82 * chasm_carve);

                vec3 l2_spine = sampleLayer2Abyss(
                    uv_L2 + lip_jitter * 0.4, px, rz.d, rz.z,
                    rz.t, rz.seed, rz.arc, heartbeat, gpu, false
                );
                float spine_gate = smoothstep(0.32, 0.72, rz.z);
                inner_col = max(g1b, l2_spine * spine_gate);
            }

            // 3D Directional Canyon Lip Wall Shading (z < 0.20) when Raytracing is ON!
            if (rt_enabled) {
                vec2 key_vec = (u_mouse.xy - uv) * vec2(aspect, 1.0);
                vec2 light_dir = normalize(key_vec + vec2(0.30, -0.45));
                float wall_band   = 1.0 - smoothstep(0.005, 0.20, rz.z);
                float wall_facing = dot(dir_to_spine, light_dir);

                // Shadowed canyon cliff wall drops into deep #070002 trench shadow!
                float wall_shade = clamp(-wall_facing + 0.15, 0.0, 1.0) * wall_band;
                inner_col = mix(inner_col, vec3(0.015, 0.000, 0.004), wall_shade * 0.85);

                // Opposite canyon cliff wall catches warm molten ember bevel + sliced PCB strata!
                float wall_lit = clamp(wall_facing + 0.10, 0.0, 1.0) * (1.0 - smoothstep(0.0, 0.11, rz.z));
                float strata = 0.45 + 0.55 * step(0.05, sin(rz.z * 230.0 + px.y * 0.35));
                vec3 lit_bevel = bloodRamp(0.50 + 0.46 * (0.45 + 0.55 * rz.arc) * strata);
                inner_col = max(inner_col, lit_bevel * wall_lit * 0.95);
            } else {
                float on_lip = 1.0 - smoothstep(0.0, 0.06, rz.z);
                vec3 lip_ember = bloodRamp(0.36 + 0.42 * rz.arc * (1.0 - smoothstep(2.6, 6.0, rz.t)));
                inner_col = max(inner_col, lip_ember * on_lip * step(0.0, dither + 0.20) * 0.72);
            }

            col = inner_col;
        }
    }

    // 8.5s..10.0s: 1-2px #3a0006 coagulated dashed byte-seam + green sync re-lock glint
    if (rz.seam > 0.01) {
        float byte_dash = step(0.32, hash11(floor(px.x / 6.0) + floor(px.y / 2.0) * 17.0));
        vec3 seam_col = vec3(0.227, 0.000, 0.024) * (0.75 + 0.25 * heartbeat); // #3a0006
        col = mix(col, max(col, seam_col), rz.seam * byte_dash * 0.90);
    }
    if (rz.relock > 0.01) {
        col = max(col, vec3(0.00, 0.86, 0.34) * rz.relock * 0.62);
    }

    // =========================================================================
    // 6. BURST "NO CARRIER" 120ms FULL-WIDTH LAYER-2 FLASH (u_extra.z >= 0.0)
    // =========================================================================
    if (u_extra.z >= 0.0 && u_extra.z <= 1.0) {
        float row_dist = abs(uv.y - u_extra.z + tear_tooth * 0.004);
        if (row_dist < 0.019) {
            float band_sel = step(bayer01, (0.019 - row_dist) / 0.006);
            if (band_sel > 0.5) {
                vec3 nc_col = sampleLayer2Abyss(uv, px, 0.96, 0.90, 0.20, 0.53, 0.95, heartbeat, gpu, true);
                vec4 nc_veil = texture(u_veil_tex, clamp(uv, vec2(0.001), vec2(0.999)));
                vec3 nc_text = sanitizeVeilAscii(nc_veil.rgb) * 1.35;
                col = hueClip(noPink(max(nc_col, nc_text)));
            }
        }
    }

    // =========================================================================
    // 7. GHOSTLY GAME OF LIFE CELLULAR SHADER BLOOM (Soft, Zero-Boxy, Zero-Pink)
    // =========================================================================
    if (life_energy > 0.008) {
        vec3 g_col = ghostlyStrainColor(l_c.b + life_alive_blur * 0.08);
        float crt_mask = (0.80 + 0.20 * cos(px.y * PI)) * (0.90 + 0.20 * dither);
        float vid_mod  = 0.52 + 0.68 * rawVideoSig(uv_L0 + life_grad * 0.010);

        float rift_keep = 1.0 - rz.open * smoothstep(0.08, 0.24, rz.z);
        vec3 ghost_glow = g_col * life_energy * crt_mask * vid_mod * rift_keep * 0.80;

        float birth = smoothstep(0.46, 0.92, life_alive_blur) * (1.0 - l_c.a) * rift_keep;
        vec3 birth_col = (l_c.b < 0.48) ? vec3(0.10, 0.94, 0.48) : vec3(0.92, 0.38, 0.04);
        ghost_glow = max(ghost_glow, birth_col * birth * 0.42 * crt_mask);

        col = max(col, ghost_glow);
    }

    // =========================================================================
    // 8. RTX 5070 HARDWARE LIGHT RAY TRACING (Vulkan VK_KHR_ray_query u_rt_gi_tex)
    //    Multi-emitter area-light soft penumbra shadows off 3D panel bezels,
    //    X11 windows & Bad Sectors + volumetric shafts + 4-ray RT AO + GGX glass!
    // =========================================================================
    if (rt_enabled) {
        // 8A. Reconstruct ray-traced light field:
        //     u_extra.x == 1.0 -> Vulkan 1.3 RT-Core hardware u_rt_gi_tex (480x270 RGBA16F)
        //     u_extra.x >= 2.0 -> Hybrid-safe native GLSL shadow-ray marcher (zero cross-GPU RTD3 wakeups!)
        vec2 gi_eps = vec2(1.0 / 480.0, 1.0 / 270.0) * 0.85;
        vec4 gi_c, gi_n, gi_s, gi_e, gi_w, rt_gi;

        if (u_extra.x < 1.5) {
            gi_c = texture(u_rt_gi_tex, uv);
            gi_n = texture(u_rt_gi_tex, clamp(uv + vec2(0.0, -gi_eps.y), vec2(0.001), vec2(0.999)));
            gi_s = texture(u_rt_gi_tex, clamp(uv + vec2(0.0,  gi_eps.y), vec2(0.001), vec2(0.999)));
            gi_e = texture(u_rt_gi_tex, clamp(uv + vec2( gi_eps.x, 0.0), vec2(0.001), vec2(0.999)));
            gi_w = texture(u_rt_gi_tex, clamp(uv + vec2(-gi_eps.x, 0.0), vec2(0.001), vec2(0.999)));
            rt_gi = gi_c * 0.44 + (gi_n + gi_s + gi_e + gi_w) * 0.14;
        } else {
            vec3 sh_irr = vec3(0.0);
            if (grad_len > 0.0012 && rift_raw.r > 0.004 && rift_raw.g > 0.01 && raw_t < 8.6) {
                float radius_uv = clamp((2.0 * length(g_eps)) / max(grad_len, 0.020), 0.032, 0.135);
                vec2 vec_to_L = dir_to_spine * max(0.962 - rift_raw.r, 0.0) * radius_uv * 1.12;
                vec2 p_L = clamp(uv + vec_to_L, vec2(0.001), vec2(0.999));
                vec4 r_L = texture(u_rift_tex, p_L);
                float tq_L = (raw_t < 2.6) ? floor(u_time * 12.0) : floor(u_time * 3.0);
                float knot_L = pow(ridge(p_L * u_res / 14.0 + tq_L * 0.35 + r_L.b * 19.0), 2.8);

                float trans = 1.0;
                float weight = 1.0;
                float acc = 0.0;
                for (int k = 1; k <= 8; ++k) {
                    float f = (float(k) - 0.35 + bayer01 * 0.14) / 8.0;
                    vec2 p_tap = clamp(uv + vec_to_L * f, vec2(0.001), vec2(0.999));
                    float l_occ = texture(u_life_tex, p_tap).r * 0.65;
                    float bezel_occ = smoothstep(0.56, 0.86, rawVideoSig(p_tap)) * 0.42;
                    trans *= (1.0 - l_occ) * (1.0 - bezel_occ);
                    vec4 r_tap = texture(u_rift_tex, p_tap);
                    acc += smoothstep(0.60, 0.96, r_tap.r) * (0.50 + 0.50 * r_tap.a) * trans * weight;
                    weight *= 0.85;
                }
                float hue_w = 0.5 + 0.5 * sin(p_L.x * 11.0 - p_L.y * 7.0 + r_L.b * 23.0);
                vec3 emit_c = mix(vec3(0.84, 0.03, 0.075), vec3(0.40, 0.055, 0.90), smoothstep(0.68, 0.92, hue_w) * 0.55);
                sh_irr += emit_c * clamp((acc * 0.24 + knot_L * trans * 0.20), 0.0, 0.65);
            }
            vec2 to_cur = u_mouse.xy - uv;
            float d_cur_asp = length(to_cur * vec2(aspect, 1.0));
            if (d_cur_asp < 0.26) {
                float c_tr = 1.0;
                for (int k = 1; k <= 4; ++k) {
                    vec2 pc = clamp(uv + to_cur * (float(k) / 4.0), vec2(0.001), vec2(0.999));
                    c_tr *= (1.0 - 0.55 * texture(u_life_tex, pc).r) * (1.0 - 0.38 * smoothstep(0.58, 0.88, rawVideoSig(pc)));
                }
                float hot_c = clamp(u_mouse.z * 1.35 + step(1.5, u_extra.y) * 0.65, 0.0, 1.0);
                vec3 c_rgb = mix(vec3(0.02, 0.84, 0.44), vec3(0.92, 0.24, 0.04), hot_c);
                sh_irr += c_rgb * exp(-pow(d_cur_asp / 0.10, 2.0)) * c_tr * (0.16 + 0.36 * u_mouse.z);
            }
            float sh_ao = clamp(1.0 - 0.45 * win_halo - 0.32 * sec_rim, 0.28, 1.0);
            rt_gi = vec4(sh_irr, sh_ao);
            gi_e = vec4(sh_irr + dir_to_spine.x * 0.05, sh_ao);
            gi_w = vec4(sh_irr - dir_to_spine.x * 0.05, sh_ao);
            gi_s = vec4(sh_irr + dir_to_spine.y * 0.05, sh_ao);
            gi_n = vec4(sh_irr - dir_to_spine.y * 0.05, sh_ao);
        }

        vec3 rt_light = noPink(max(rt_gi.rgb, vec3(0.0)));
        float rt_ao   = clamp(rt_gi.a, 0.0, 1.0);
        float rt_mag  = maxc(rt_light);

        // 8B. Analytic CRT / PCB / Fracture Relief Normal N
        float lip_slope_mask = 0.0;
        if (rift_raw.r > 0.01 && rift_raw.g > 0.004 && raw_t < 8.65) {
            lip_slope_mask = exp(-pow((rz.dj - rz.tau) / 0.055, 2.0));
        }
        float sec_slope_mask = exp(-pow(sec_margin / 0.035, 2.0));

        vec2 lip_grad = dir_to_spine * lip_slope_mask * 2.3;
        vec2 life_n_grad = life_grad * 1.45;
        float on_glass = (rz.open > 0.5 || is_bad_sector > 0.5) ? 0.18 : 1.0;
        float scan_rib = cos(px.y * PI) * 0.18 * on_glass;

        vec2 sig_grad = vec2(
            rawVideoSig(uv_L0 + vec2(g_eps.x * 0.5, 0.0)) - rawVideoSig(uv_L0 - vec2(g_eps.x * 0.5, 0.0)),
            rawVideoSig(uv_L0 + vec2(0.0, g_eps.y * 0.5)) - rawVideoSig(uv_L0 - vec2(0.0, g_eps.y * 0.5))
        ) * 0.72;
        float trace_relief = clamp(length(sig_grad) * 2.8, 0.0, 1.0);

        vec2 total_grad = lip_grad - life_n_grad - sig_grad + vec2(0.0, scan_rib);
        vec3 N = normalize(vec3(total_grad.x, total_grad.y, 1.0));
        vec3 V = normalize(-view_dir);

        // Estimate dominant incoming RT irradiance gradient so circuit traces & bezels catch side-light!
        vec2 gi_dir_2d = vec2(maxc(gi_e.rgb) - maxc(gi_w.rgb), maxc(gi_s.rgb) - maxc(gi_n.rgb));
        float gi_dir_len = length(gi_dir_2d);
        vec3 L_gi = (gi_dir_len > 0.0008)
            ? normalize(vec3((gi_dir_2d / gi_dir_len) * 0.85, 0.52))
            : normalize(vec3((u_mouse.xy - uv) * vec2(aspect, 1.0), 0.25));
        float relief_lambert = clamp(dot(N, L_gi), 0.15, 1.0);

        // 8C. Hardware RT 4-Ray Contact Ambient Occlusion around 3D slotted bezels, windows & Bad Sectors!
        //     Keep entire recessed Gen 1B shelf + chasm crisp & un-washed-out (inner_shield starts at z=0.01)!
        float inner_shield = rz.open * smoothstep(0.008, 0.085, rz.z);
        float outer_rt_mask = 1.0 - inner_shield;

        float ao_factor = mix(mix(0.28, 1.0, rt_ao), 1.0, inner_shield);
        col *= ao_factor;

        // 8D. Physically-Based Ray-Traced Relief Illumination & Slotted Volumetric Shadow Shafts!
        //     Zero DC pedestal on shadowed voids: diffuse bounce hits physical traces/bezels/lips,
        //     while unoccluded volumetric shafts stream dramatically through slotted 3D panel bezels!
        float struct_mask = clamp(max(trace_relief, max(lip_slope_mask, sec_slope_mask)), 0.0, 1.0);
        vec3 surf_albedo  = col * 0.85 + vec3(0.18, 0.16, 0.20) * struct_mask;
        vec3 diffuse_bounce = surf_albedo * rt_light * (1.15 + 1.55 * relief_lambert) * outer_rt_mask;

        // Atmospheric volumetric scatter shafts streaming through slotted 3D bezels & Life lattice!
        float shaft_gate = smoothstep(0.016, 0.24, rt_mag);
        float fog_tex    = 0.80 + 0.20 * vnoise(uv * vec2(26.0, 15.0) + vec2(u_time * 0.18, -u_time * 0.11));
        vec3 vol_shaft   = min(rt_light, vec3(0.75)) * shaft_gate * fog_tex * (0.62 + 0.08 * dither) * outer_rt_mask;

        col = col + diffuse_bounce + vol_shaft;

        // 8E. Shadow-Gated Schlick Fresnel & GGX Microfacet Specular on Wet-Obsidian CRT Glass!
        vec3 L1_raw = vec3((u_mouse.xy - uv) * vec2(aspect, 1.0), 0.07);
        float L1_d  = length(L1_raw);
        vec3 L1     = L1_raw / max(L1_d, 0.001);
        float att1  = 1.0 / (1.0 + 10.0 * L1_d * L1_d);

        vec3 L2    = (grad_len > 0.0012)
            ? normalize(vec3(dir_to_spine * vec2(aspect, 1.0) * 0.85, 0.20))
            : L_gi;
        float rt_vis_gate = clamp(rt_mag * 2.8, 0.0, 1.25);
        float att2 = clamp(rt_vis_gate * 0.85 + lip_slope_mask * 0.90 + rz.moat * 0.55, 0.0, 1.15);

        float NdotV   = max(dot(N, V), 0.001);
        float fresnel = 0.045 + 0.955 * pow(1.0 - NdotV, 5.0);
        float rough   = mix(0.26, 0.11, clamp(lip_slope_mask + sec_slope_mask * 0.65 + trace_relief * 0.45, 0.0, 1.0));

        vec3 H1 = normalize(V + L1);
        float sp1 = ggxD(max(dot(N, H1), 0.0), rough) * max(dot(N, L1), 0.0) * att1 * clamp(0.25 + rt_vis_gate, 0.0, 1.15);

        vec3 H2 = normalize(V + L2);
        float sp2 = ggxD(max(dot(N, H2), 0.0), rough) * max(dot(N, L2), 0.0) * att2;

        float feat = clamp(lip_slope_mask * 1.15 + sec_slope_mask * 0.78 + trace_relief * 0.70 + life_energy * 0.55 + maxc(col) * 0.25, 0.0, 1.0);
        float spec = clamp((sp1 * 0.52 + sp2 * 0.78) * (0.30 + 0.70 * fresnel) * feat, 0.0, 1.25);

        if (spec > 0.006) {
            // Tinted by actual ray-traced incident light hue when illuminated, or bloodRamp ember on fracture lips!
            vec3 rt_tint = (rt_mag > 0.025) ? normalize(rt_light + vec3(0.001)) : bloodRamp(0.72);
            float hot = clamp(0.45 + 0.52 * min(spec, 1.0) + 0.18 * rz.arc, 0.0, 0.98);
            vec3 ember_glint = mix(bloodRamp(hot), rt_tint * 1.15, 0.42) * min(spec, 1.0);
            float warm_zone  = clamp(att2 * 0.85 + att1 * 0.85 + is_bad_sector * 0.55, 0.0, 1.0);
            vec3 cool_glint  = vec3(0.00, 0.76, 0.42) * min(spec, 0.65) * 0.52;
            col = max(col, noPink(mix(cool_glint, ember_glint, warm_zone)));
        }

        // Sharp wet-obsidian grazing rim reflection right on the torn glass fracture lip
        float rim_glint = (0.35 + 0.65 * fresnel) * lip_slope_mask * (0.45 + 0.55 * max(att1, att2));
        col = max(col, bloodRamp(0.52 + 0.42 * rz.arc) * rim_glint * 0.88);
    }

    // =========================================================================
    // 9. CURSOR CRT SCAN-GLINT (Phosphor-Emerald / Warm Ember, Zero CP2077 Cyan!)
    // =========================================================================
    vec2 d_cur = (uv - u_mouse.xy) * vec2(aspect, 1.0);
    float cur_glint = exp(-pow(length(d_cur) / 0.0052, 2.0)) * (0.16 + 0.26 * u_mouse.z);
    vec3 cur_col = mix(vec3(0.00, 0.80, 0.45), bloodRamp(0.78), clamp(u_mouse.z * 1.4 + step(1.5, u_extra.y) * 0.6, 0.0, 1.0));
    col = max(col, cur_col * cur_glint);

    // =========================================================================
    // 10. WINDOW-AWARE ICE LUMA GOVERNOR & 1px CRIMSON FRAME SEAMS
    // =========================================================================
    if (win_inside > 0.005) {
        float y_lum = dot(col, vec3(0.2126, 0.7152, 0.0722));
        if (y_lum > 0.24) {
            float y_gov = 0.24 + (y_lum - 0.24) * 0.46;
            col *= mix(1.0, y_gov / max(y_lum, 0.001), win_inside * 0.82);
        } else {
            col *= mix(1.0, 0.88, win_inside * 0.55);
        }
    }
    if (win_frame > 0.01) {
        float cross_hot = clamp(step(0.01, rz.d) * step(0.004, rz.age) + is_bad_sector * 0.75, 0.0, 1.0);
        if (cross_hot > 0.05) {
            vec3 seam_frame = bloodRamp(0.42 + 0.30 * rz.arc) * win_frame * cross_hot * (0.70 + 0.30 * step(0.0, dither));
            col = max(col, seam_frame);
        }
    }

    col = hueClip(noPink(col));

    // =========================================================================
    // 11. TOP-LEFT & TOP-RIGHT BBS HUD TELEMETRY BOXES (Premultiplied Alpha)
    // =========================================================================
    vec4 hud = texture(u_hud_tex, uv);
    col = col * (1.0 - hud.a) + hud.rgb;

    frag_color = vec4(clamp(col, 0.0, 1.0), 1.0);
}
