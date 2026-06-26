// orb_shader.vert — vertex displacement for the JARVIS presence orb
// ==================================================================
// Qt Quick 3D CustomMaterial vertex stage. No #version / no main() — Qt wraps
// this MAIN() and recomputes the clip position from the (possibly displaced)
// VERTEX automatically.
//
// UNIFORMS — declared as CustomMaterial properties in orb.qml. Every name here
// MUST be spelled identically in orb.qml (a mismatch silently renders flat):
//   uJitter        float   displacement amplitude (0 = flat/idle .. higher = chaotic)
//   uTime          float   seconds, ever-increasing — scrolls the noise field
//   uRipple        float   listening-ripple progress 0..1 (<0 = inactive)
//   uRippleOrigin  vec3    unit object-space point the ripple emanates from
//
// VARYINGS — must match orb_shader.frag exactly:
//   vDisp          float   signed displacement applied this vertex (crest tinting)

VARYING float vDisp;

// Hash-based value noise. No texture fetch — keeps the orb's own GPU cost low so
// it doesn't fight Ollama for the A2000's 6GB headroom (the load it visualises).
float hash13(vec3 p) {
    p = fract(p * 0.1031);
    p += dot(p, p.yzx + 33.33);
    return fract((p.x + p.y) * p.z);
}

float vnoise(vec3 p) {
    vec3 i = floor(p);
    vec3 f = fract(p);
    f = f * f * (3.0 - 2.0 * f);              // smoothstep interpolation
    float n000 = hash13(i + vec3(0.0, 0.0, 0.0));
    float n100 = hash13(i + vec3(1.0, 0.0, 0.0));
    float n010 = hash13(i + vec3(0.0, 1.0, 0.0));
    float n110 = hash13(i + vec3(1.0, 1.0, 0.0));
    float n001 = hash13(i + vec3(0.0, 0.0, 1.0));
    float n101 = hash13(i + vec3(1.0, 0.0, 1.0));
    float n011 = hash13(i + vec3(0.0, 1.0, 1.0));
    float n111 = hash13(i + vec3(1.0, 1.0, 1.0));
    float nx00 = mix(n000, n100, f.x);
    float nx10 = mix(n010, n110, f.x);
    float nx01 = mix(n001, n101, f.x);
    float nx11 = mix(n011, n111, f.x);
    float nxy0 = mix(nx00, nx10, f.y);
    float nxy1 = mix(nx01, nx11, f.y);
    return mix(nxy0, nxy1, f.z) * 2.0 - 1.0;  // remap to -1..1
}

void MAIN() {
    vec3 dir = normalize(NORMAL);

    // Chaotic jitter: noise sampled in object space, scrolling with time. uJitter
    // (set from telemetry in orb.qml) is the amplitude knob — this is the "it just
    // feels right" coupling between GPU load and how agitated the orb looks.
    float n = vnoise(VERTEX * 0.04 + vec3(uTime * 0.6));
    float disp = uJitter * n;

    // Listening ripple: a travelling gaussian ring whose position is the angular
    // distance from uRippleOrigin. Only contributes while uRipple is in [0,1]; it
    // sweeps from the origin pole outward and eases out as it goes.
    if (uRipple >= 0.0) {
        float ang = acos(clamp(dot(dir, normalize(uRippleOrigin)), -1.0, 1.0)); // 0..pi
        float ringPos = uRipple * 3.14159265;        // crest position pole -> pole
        float d = ang - ringPos;
        float ring = exp(-(d * d) / 0.05);           // crest width
        float fade = 1.0 - uRipple;                  // ripple eases out as it travels
        disp += ring * fade * 0.16;                  // crest height (object-relative)
    }

    // Scale to object units (mesh radius ~90) and displace along the facet normal.
    VERTEX += dir * disp * 100.0;
    vDisp = disp;
}
