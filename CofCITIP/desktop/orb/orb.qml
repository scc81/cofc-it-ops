// orb.qml — JARVIS presence orb (Qt Quick 3D scene)
// =================================================
// CofCITIP — JARVIS Phase 3 desktop UI (Session C)
//
// One faceted icosahedron (orb_geometry.IcosahedronGeometry) whose vertices are
// displaced along their normals by orb_shader.vert, tinted by orb_shader.frag.
// Everything the orb reacts to comes from ONE source of truth: the TelemetryBridge
// exposed to QML as the `telemetry` context property (see orb_widget.py). The orb
// never invents its own data path.
//
// State surface (telemetry.orbState — set from Python, demo timer now, jarvis-core
// later): 0 idle | 1 listening | 2 processing | 3 spillover.

import QtQuick
import QtQuick3D
import JarvisOrb

Item {
    id: root

    // ── live bindings from the bridge (guarded so orb.qml still loads standalone
    //    e.g. in qml/qmlscene with no `telemetry` context property present) ──
    property real gpuLoad: telemetry ? telemetry.gpuLoad : 0.0          // 0..100
    property real cpuLoad: telemetry ? telemetry.cpuLoad : 0.0          // 0..100
    property bool cpuSpillover: telemetry ? telemetry.cpuSpillover : false
    property int  orbState: telemetry ? telemetry.orbState : 0          // 0..3

    // ── animated internals ──
    property real animTime: 0.0           // seconds, ever-increasing -> shader uTime
    property real rippleProgress: -1.0    // <0 = inactive; 0..1 = ripple travelling

    // ── CofC palette + the off-palette warning tone ──
    readonly property color cofcGoldMuted: Qt.rgba(0.55, 0.46, 0.28, 1.0) // idle: muted gold
    readonly property color cofcGold:      "#efbb3c"                      // processing: active gold
    readonly property color cofcMaroon:    "#660000"                      // listening: maroon
    // OFF-PALETTE warning amber — deliberately NOT gold (#efbb3c) and NOT maroon
    // (#660000). A vivid orange so the alert state can never be misread as a brand
    // colour (explicit design requirement from jarvis_phase3_native_ui.md).
    readonly property color warnAmber:     "#ff6a00"

    property color orbColor: cofcGoldMuted

    function _targetColor() {
        switch (orbState) {
        case 1: return cofcMaroon;     // listening
        case 2: return cofcGold;       // processing
        case 3: return warnAmber;      // spillover (off-palette)
        default: return cofcGoldMuted; // idle
        }
    }

    // ── jitterAmplitude: the single most "feel"-tuned value in this scene ──
    //
    // Each state has a fixed FLOOR so the four states are always visibly distinct
    // by amplitude alone — even on a box with no live GPU load (e.g. the demo
    // machine, or an idle A2000), where the live terms below are 0. ON TOP of that
    // floor, processing/spillover add the LIVE telemetry term continuously, so on
    // BB during real inference the orb's agitation tracks actual GPU/CPU pressure.
    //   idle       : tiny floor — the orb breathes, never dead-flat.
    //   listening  : low/steady — the RIPPLE (not jitter) carries this state.
    //   processing : procFloor + gpuLoad term  (primary live data binding).
    //   spillover  : spillFloor + gpuLoad term + cpuLoad term. cpuGain > gpuGain
    //                and spillFloor > procFloor, so spillover is ALWAYS more
    //                chaotic than processing — a model fell out of VRAM onto the
    //                CPU and the orb should look stressed about it.
    readonly property real idleFloor:  0.015
    readonly property real procFloor:  0.05
    readonly property real spillFloor: 0.11
    readonly property real gpuGain:    0.085   // full GPU adds this much on top
    readonly property real cpuGain:    0.12    // intentionally > gpuGain (see above)

    function _targetJitter() {
        switch (orbState) {
        case 1: return idleFloor * 1.5;                                      // listening
        case 2: return procFloor + (gpuLoad / 100.0) * gpuGain;             // processing (live)
        case 3: return spillFloor + (gpuLoad / 100.0) * gpuGain
                       + (cpuLoad / 100.0) * cpuGain;                        // spillover (live, harder)
        default: return idleFloor;                                          // idle
        }
    }

    property real jitterAmplitude: idleFloor

    // smooth the discrete jumps so state changes ease rather than snap
    Behavior on orbColor { ColorAnimation { duration: 450 } }
    Behavior on jitterAmplitude { NumberAnimation { duration: 350; easing.type: Easing.OutCubic } }

    onOrbStateChanged: {
        orbColor = _targetColor();
        jitterAmplitude = _targetJitter();
        if (orbState === 1)        // entering "listening" fires exactly one ripple
            rippleAnim.restart();
    }
    // keep processing/spillover tracking LIVE telemetry between state changes
    onGpuLoadChanged: if (orbState >= 2) jitterAmplitude = _targetJitter();
    onCpuLoadChanged: if (orbState === 3) jitterAmplitude = _targetJitter();

    Component.onCompleted: {
        orbColor = _targetColor();
        jitterAmplitude = _targetJitter();
    }

    // animTime driver — a plain Timer (version-safe vs FrameAnimation); ~60Hz is
    // plenty to scroll the noise field smoothly and costs almost nothing.
    Timer {
        interval: 16; running: true; repeat: true
        onTriggered: root.animTime += 0.016
    }

    // one-shot ripple sweep, fired on entering the listening state
    NumberAnimation {
        id: rippleAnim
        target: root
        property: "rippleProgress"
        from: 0.0; to: 1.0
        duration: 1100
        easing.type: Easing.OutQuad
        onFinished: root.rippleProgress = -1.0
    }

    View3D {
        anchors.fill: parent

        environment: SceneEnvironment {
            clearColor: "transparent"
            backgroundMode: SceneEnvironment.Transparent
            antialiasingMode: SceneEnvironment.MSAA
            antialiasingQuality: SceneEnvironment.High
        }

        PerspectiveCamera {
            id: cam
            position: Qt.vector3d(0, 0, 360)
            clipNear: 1
            clipFar: 2000
        }

        DirectionalLight {            // key light
            eulerRotation: Qt.vector3d(-30, -25, 0)
            brightness: 1.0
        }
        DirectionalLight {            // soft fill so back facets aren't pure black
            eulerRotation: Qt.vector3d(120, 40, 0)
            brightness: 0.35
        }

        Model {
            id: orbModel
            // Faceted icosahedron (flat per-face normals) — see orb_geometry.py.
            geometry: IcosahedronGeometry { subdivisions: 1; radius: 90 }

            // Idle slow constant rotation — runs in every state, so the orb is
            // never perfectly still. ~24s/revolution reads calm at idle.
            NumberAnimation on eulerRotation.y {
                from: 0; to: 360; duration: 24000
                loops: Animation.Infinite; running: true
            }
            eulerRotation.x: 12

            materials: CustomMaterial {
                shadingMode: CustomMaterial.Shaded
                cullMode: Material.NoCulling   // displacement can flip facets; keep solid
                vertexShader: "orb_shader.vert"
                fragmentShader: "orb_shader.frag"

                // ── shader uniforms — names cross-checked against orb_shader.vert
                //    and orb_shader.frag (PositionSemantic of QML/shader binding) ──
                property real uJitter: root.jitterAmplitude
                property real uTime: root.animTime
                property real uRipple: root.rippleProgress
                property vector3d uRippleOrigin: Qt.vector3d(0.0, 1.0, 0.0)
                property color uBaseColor: root.orbColor
                property real uEmissiveStrength: root.orbState === 0 ? 0.10 : 0.22
            }
        }
    }
}
