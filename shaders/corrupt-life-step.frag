#version 330 core

in vec2 v_uv;
out vec4 frag_life;

uniform sampler2D u_prev_life;
uniform sampler2D u_rift_tex;
uniform vec2  u_life_res;
uniform float u_time;
uniform vec4  u_loads; // x=cpu, y=gpu, z=ram, w=burst
uniform vec4  u_mouse; // x=uv_x, y=uv_y, z=speed_norm, w=aspect
uniform vec4  u_win[8];
uniform int   u_win_count;
uniform vec4  u_extra; // x=raytrace, y=dwell_sec, z=no_carrier_y, w=scar_remap_seed

float hash13(vec3 p3) {
    p3 = fract(p3 * 0.1031);
    p3 += dot(p3, p3.zyx + 31.32);
    return fract((p3.x + p3.y) * p3.z);
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

// =============================================================================
// Shared Shattered Rift Zones Math (matches corrupt-veil.frag 1:1)
// u_rift_tex: R = tapered linear profile, G = 10s age (1->0), B = seed, A = arc
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
void evalWindowField(vec2 uv, float aspect, out float win_inside, out float win_halo, out float win_border) {
    win_inside = 0.0;
    win_halo   = 0.0;
    win_border = 0.0;
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
        win_border = max(win_border, exp(-pow(sdist / 0.0055, 2.0)));
    }
}

float evalBadSectorMargin(vec2 uv, float t, float load_mix, float ram, float aspect) {
    vec2 grid = vec2(32.0, 18.0);
    vec2 block_id = floor(uv * grid);
    vec2 block_uv = (block_id + 0.5) / grid;

    float w_in_b, w_halo_b, w_bdr_b;
    evalWindowField(block_uv, aspect, w_in_b, w_halo_b, w_bdr_b);

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

    if (u_extra.w > 0.001) {
        float r_rem = hash21(block_id + floor(u_extra.w * 13.0) + 7.3);
        if (r_rem > 0.92 - 0.06 * ram) {
            F += 0.32 * clamp(u_extra.w * 3.0, 0.0, 1.0);
        }
    }

    float cov = 0.08 + 0.78 * smoothstep(0.22, 0.88, load_mix);
    return F - (1.0 - cov);
}

void main() {
    vec2 texel = 1.0 / u_life_res;
    vec2 px = floor(v_uv * u_life_res);
    float aspect = max(u_mouse.w, 0.5);

    vec4 self_c = texture(u_prev_life, v_uv);
    float alive  = step(0.5, self_c.r);
    float ghost  = self_c.g;
    float strain = self_c.b;
    float stasis = self_c.a;

    // 1. Count 8 Moore neighbors + gather neighbor strain
    float n_sum = 0.0;
    float strain_acc = 0.0;
    for (int dy = -1; dy <= 1; ++dy) {
        for (int dx = -1; dx <= 1; ++dx) {
            if (dx == 0 && dy == 0) continue;
            vec2 n_uv = clamp(v_uv + vec2(float(dx), float(dy)) * texel, vec2(0.0005), vec2(0.9995));
            vec4 nc = texture(u_prev_life, n_uv);
            float na = step(0.5, nc.r);
            n_sum += na;
            strain_acc += na * nc.b;
        }
    }

    int n = int(round(n_sum));
    float next_alive = 0.0;
    if (alive > 0.5) {
        next_alive = (n == 2 || n == 3) ? 1.0 : 0.0;
    } else {
        next_alive = (n == 3) ? 1.0 : 0.0;
    }

    ivec2 cur_cell = ivec2(floor(u_mouse.xy * u_life_res));
    ivec2 d_cell   = abs(ivec2(px) - cur_cell);
    bool in_pulsar_box = (u_extra.y > 1.5) && (max(d_cell.x, d_cell.y) <= 8);

    // Reap unchanging static 2x2 blocks so ghostly glider/oscillator motion dominates
    if (!in_pulsar_box && alive > 0.5 && next_alive > 0.5 && n == 3) {
        stasis = min(1.0, stasis + 0.026);
        if (stasis > 0.76) {
            next_alive = 0.0;
            stasis = 0.0;
        }
    } else {
        stasis = 0.0;
    }

    // 2. Decoupled 10s Rift Lip Seed Band — tracks exact shattered cyber-lightning fracture contour!
    vec4 rift = texture(u_rift_tex, v_uv);
    float t_raw = (1.0 - rift.g) * 10.0;
    float jag_seed = evalRiftFractureJag(v_uv, px * 3.0, rift.b, t_raw, u_time);
    Rift rz = riftZones(rift, jag_seed);

    float lip_dist   = abs(rz.dj - rz.tau);
    float lip_active = step(0.004, rz.age) * step(0.004, rz.d) * (1.0 - smoothstep(8.3, 8.9, rz.t));
    float cut_ramp   = smoothstep(0.04, 0.50, rz.t);
    float lip_band   = (1.0 - smoothstep(0.015, 0.09, lip_dist)) * lip_active * (0.42 + 0.58 * cut_ramp);

    // 3. Delicate Ghost Life Seeding along Bad Sector Perimeters & Window Borders
    float load_mix = clamp(0.42 * u_loads.x + 0.36 * u_loads.y + 0.22 * u_loads.z, 0.0, 1.0);
    float win_inside, win_halo, win_border;
    evalWindowField(v_uv, aspect, win_inside, win_halo, win_border);

    float sec_margin = evalBadSectorMargin(v_uv, u_time, load_mix, u_loads.z, aspect);
    float sec_perim  = 1.0 - smoothstep(0.006, 0.048, abs(sec_margin));

    // Direct mouse brush when moving
    vec2 d_mouse = (v_uv - u_mouse.xy) * vec2(aspect, 1.0);
    float mouse_dist = length(d_mouse);
    float brush_zone = exp(-pow(mouse_dist / (0.015 + 0.024 * u_mouse.z), 2.0)) * clamp(u_mouse.z * 2.4, 0.0, 1.0);

    float rnd = hash13(vec3(px, floor(u_time * 30.0)));
    float seed_prob = brush_zone * 0.38
                    + lip_band   * (0.060 + 0.090 * load_mix + 0.060 * rz.arc)
                    + sec_perim  * (0.005 + 0.015 * load_mix)
                    + win_border * (0.004 + 0.013 * load_mix) * (1.0 - 0.5 * win_inside);

    if (!in_pulsar_box && rnd < seed_prob) {
        next_alive = 1.0;
        stasis = 0.0;
    }

    // 4. Cursor Dwell > 1.5s: True Conway B3/S23 Period-3 Pulsar Oscillator around u_mouse.xy
    if (in_pulsar_box) {
        float dwell_phase = u_extra.y - 1.5;
        if (dwell_phase < 0.14 || fract(dwell_phase / 3.2) < 0.035) {
            bool on_bar_x = (d_cell.x >= 2 && d_cell.x <= 4) && (d_cell.y == 1 || d_cell.y == 6);
            bool on_bar_y = (d_cell.y >= 2 && d_cell.y <= 4) && (d_cell.x == 1 || d_cell.x == 6);
            next_alive = (on_bar_x || on_bar_y) ? 1.0 : 0.0;
            stasis = 0.0;
        }
    }

    // 5. Clear deep inner spine (z > 0.25) so Gen 1B mid-zone & Gen 3 Blood Spine stay razor-sharp
    if (rz.open > 0.5 && rz.z > 0.25 && brush_zone < 0.18 && !in_pulsar_box) {
        next_alive = 0.0;
        ghost *= 0.45;
    }

    // 6. Evaporate stray gliders outside tight lip/moat band so moat foam stays crisp & narrow
    float tight_moat = smoothstep(rz.tau - 0.13, rz.tau, rz.dj) * (1.0 - rz.open) * lip_active;
    float anchor = max(max(lip_band, tight_moat), max(max(sec_perim * 0.65, win_border * 0.55), brush_zone));
    if (!in_pulsar_box && anchor < 0.04 && next_alive > 0.5) {
        float keep_prob = 0.75 + 0.09 * load_mix;
        if (hash13(vec3(px + 19.7, floor(u_time * 23.0))) > keep_prob) {
            next_alive = 0.0;
        }
    }

    // 7. Zero-Pink Strain Encoding:
    //    [0.00..0.48] = Outer Phosphor / Phosphor-Emerald
    //    [0.52..1.00] = Inner Dried Arterial Crimson / Rust-Ember
    if (next_alive > 0.5 && alive < 0.5) {
        float mut = hash13(vec3(px * 1.7, floor(u_time * 15.0)));
        if (rz.d > 0.01 && rz.age > 0.004) {
            float inner_side = smoothstep(rz.tau - 0.06, rz.tau + 0.04, rz.dj);
            strain = mix(0.08 + 0.30 * mut, 0.60 + 0.36 * max(mut, rz.arc), inner_side);
        } else if (in_pulsar_box) {
            strain = (d_cell.x == 1 || d_cell.y == 1) ? 0.70 : 0.18;
        } else if (n_sum > 0.5 && mut > 0.32) {
            strain = strain_acc / n_sum;
        } else {
            float hot_bias = clamp(0.55 * u_loads.x + 0.45 * u_loads.w, 0.0, 1.0);
            strain = (mut < 0.30 + 0.42 * hot_bias) ? (0.58 + 0.34 * mut) : (0.06 + 0.34 * mut);
        }
    }

    // 8. Smooth ghostly persistence trail
    float next_ghost = (next_alive > 0.5)
        ? min(1.0, ghost * 0.44 + 0.74)
        : ghost * (0.84 + 0.06 * lip_band);
    if (next_ghost < 0.008) next_ghost = 0.0;

    frag_life = vec4(next_alive, next_ghost, clamp(strain, 0.0, 1.0), stasis);
}
