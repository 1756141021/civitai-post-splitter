"""Fingerprint hardening init script for XHS browser context."""

FINGERPRINT_INIT_SCRIPT = """
// navigator.languages — must look like a real Chinese user's browser
Object.defineProperty(navigator, 'languages', {
    get: () => ['zh-CN', 'zh', 'en-US', 'en']
});

// Canvas fingerprint noise — flip 1 pixel bit at a random position
const _origToDataURL = HTMLCanvasElement.prototype.toDataURL;
HTMLCanvasElement.prototype.toDataURL = function(type) {
    const ctx = this.getContext('2d');
    if (ctx && this.width > 16 && this.height > 16) {
        try {
            const rx = Math.floor(Math.random() * this.width);
            const ry = Math.floor(Math.random() * this.height);
            const pixel = ctx.getImageData(rx, ry, 1, 1);
            pixel.data[0] = pixel.data[0] ^ (Math.random() > 0.5 ? 1 : 0);
            ctx.putImageData(pixel, rx, ry);
        } catch(e) {}
    }
    return _origToDataURL.apply(this, arguments);
};

// WebGL renderer spoofing — common Intel UHD string
const _getParam = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(param) {
    if (param === 37445) return 'Google Inc. (Intel)';
    if (param === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)';
    return _getParam.apply(this, arguments);
};
if (typeof WebGL2RenderingContext !== 'undefined') {
    const _getParam2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function(param) {
        if (param === 37445) return 'Google Inc. (Intel)';
        if (param === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)';
        return _getParam2.apply(this, arguments);
    };
}

// navigator.webdriver — belt-and-suspenders with Patchright
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

// Permissions API — automation browsers return inconsistent results
if (window.Permissions && window.Permissions.prototype.query) {
    const _origQuery = window.Permissions.prototype.query;
    window.Permissions.prototype.query = function(parameters) {
        if (parameters.name === 'notifications') {
            return Promise.resolve({state: Notification.permission});
        }
        return _origQuery.apply(this, arguments);
    };
}

// navigator.connection — automation often lacks this
if (!navigator.connection) {
    Object.defineProperty(navigator, 'connection', {
        get: () => ({
            effectiveType: '4g',
            rtt: 50,
            downlink: 10,
            saveData: false
        })
    });
}
"""
