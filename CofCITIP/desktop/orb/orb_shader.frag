// orb_shader.frag — surface shading for the JARVIS presence orb
// =============================================================
// Qt Quick 3D CustomMaterial fragment stage, shadingMode: Shaded — so we set the
// PBR outputs (BASE_COLOR / METALNESS / ROUGHNESS / EMISSIVE_COLOR) and Qt's
// lighting does the rest. Flat per-face normals come from the mesh geometry, so
// the lighting itself reads faceted.
//
// UNIFORMS — declared as CustomMaterial properties in orb.qml. Every name here
// MUST be spelled identically in orb.qml:
//   uBaseColor         vec4   current state colour (idle muted-gold / listening
//                             maroon / processing gold / spillover amber)
//   uEmissiveStrength  float  base self-illumination so the orb reads on the dark UI
//
// VARYINGS — must match orb_shader.vert exactly:
//   vDisp              float  signed displacement from the vertex stage

VARYING float vDisp;

void MAIN() {
    BASE_COLOR = uBaseColor;
    METALNESS = 0.0;
    ROUGHNESS = 0.38;

    // Crest highlight: outward-displaced facets glow a touch brighter so the
    // jitter and the listening ripple still read on facets that direct light
    // isn't currently catching.
    float crest = clamp(vDisp * 6.0, 0.0, 1.0);
    EMISSIVE_COLOR = uBaseColor.rgb * (uEmissiveStrength + crest * 0.25);
}
