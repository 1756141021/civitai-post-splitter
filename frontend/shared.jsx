// shared.jsx — runtime helpers shared by MonoSingleApp
// • M       : color palette + font stacks
// • TIcon   : stroke-icon set used throughout the UI
// • MOCK_*  : seed data for tasks + log (replace with real bindings)
// • Mono*   : small layout primitives reused inside MonoSingleApp
// One-time CSS injection lives at the bottom — guarded by an id check so
// re-loading the file in dev never duplicates the <style> tag.

const M = {
  bg:        '#fafaf7',
  bg2:       '#f3f2ed',
  panel:     '#ffffff',
  ink:       '#171513',
  ink2:      '#3a3733',
  inkDim:    '#6b6660',
  inkFaint:  '#a39d95',
  line:      '#e6e3dc',
  lineSoft:  '#efece5',
  accent:    '#e2552b',
  accentSoft:'#fde9df',
  ok:        '#2f7d3a',
  warn:      '#a36a00',
  red:       '#b13b2c',
  display: 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif',
  mono:    '"IBM Plex Mono", "JetBrains Mono", ui-monospace, monospace',
};

// ─── Stroke icons ─────────────────────────────────────────────
function TIcon({ name, size = 16, color = "currentColor" }) {
  const p = { width: size, height: size, viewBox: "0 0 24 24", fill: "none", stroke: color, strokeWidth: 1.8, strokeLinecap: "round", strokeLinejoin: "round" };
  switch (name) {
    case "split":   return <svg {...p}><path d="M6 3v6a3 3 0 0 0 3 3h6a3 3 0 0 1 3 3v6"/><path d="M3 6l3-3 3 3"/><path d="M15 18l3 3 3-3"/></svg>;
    case "upload":  return <svg {...p}><path d="M12 15V3"/><path d="M7 8l5-5 5 5"/><path d="M3 17v2a3 3 0 0 0 3 3h12a3 3 0 0 0 3-3v-2"/></svg>;
    case "image":   return <svg {...p}><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>;
    case "shield":  return <svg {...p}><path d="M12 3l8 3v6c0 5-3.5 8.5-8 9-4.5-.5-8-4-8-9V6l8-3z"/><path d="M9 12l2 2 4-4"/></svg>;
    case "refresh": return <svg {...p}><path d="M3 12a9 9 0 0 1 15.5-6.3L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15.5 6.3L3 16"/><path d="M3 21v-5h5"/></svg>;
    case "play":    return <svg {...p}><path d="M6 4l14 8-14 8z"/></svg>;
    case "pause":   return <svg {...p}><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>;
    case "stop":    return <svg {...p}><rect x="5" y="5" width="14" height="14" rx="2"/></svg>;
    case "x":       return <svg {...p}><path d="M6 6l12 12M18 6L6 18"/></svg>;
    case "folder":  return <svg {...p}><path d="M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>;
    default: return null;
  }
}
function MIcon(props) { return <TIcon {...props} />; }

// ─── Mock data — replace with real bindings ────────────────────
const MOCK_TASKS = [
  { id: 'q-1', title: 'post_88241_arknights_w', status: 'running', progress: 0.62, target: 'Civitai + Pixiv', count: '7 / 12 imgs', eta: '00:42' },
  { id: 'q-2', title: 'post_88240_genshin_set', status: 'running', progress: 0.18, target: 'Pixiv only',      count: '2 / 11 imgs', eta: '02:08' },
  { id: 'q-3', title: 'split_post_88239',       status: 'queued',  progress: 0,    target: 'Local',           count: '— / 9 imgs',  eta: '—'     },
  { id: 'q-4', title: 'post_88235_blue_archive',status: 'queued',  progress: 0,    target: 'Civitai + Pixiv', count: '— / 6 imgs',  eta: '—'     },
  { id: 'q-5', title: 'post_88231_hsr_topaz',   status: 'done',    progress: 1,    target: 'Civitai + Pixiv', count: '8 / 8 imgs',  eta: 'done'  },
  { id: 'q-6', title: 'post_88229_nikke_rapi',  status: 'done',    progress: 1,    target: 'Pixiv only',      count: '5 / 5 imgs',  eta: 'done'  },
  { id: 'q-7', title: 'post_88224_zzz_ellen',   status: 'failed',  progress: 0.34, target: 'Civitai + Pixiv', count: '3 / 9 imgs',  eta: 'retry' },
];

const MOCK_LOG = [
  { t: '14:02:11.043', lvl: 'INFO', src: 'splitter', msg: 'Detected 12 images in post_88241' },
  { t: '14:02:11.211', lvl: 'INFO', src: 'splitter', msg: 'Split → 12 single-image posts queued' },
  { t: '14:02:12.560', lvl: 'INFO', src: 'civitai',  msg: 'Auth OK · session refreshed' },
  { t: '14:02:13.009', lvl: 'INFO', src: 'civitai',  msg: 'Uploading 1/12 → arknights_w_01.png (4.2 MB)' },
  { t: '14:02:14.880', lvl: 'OK',   src: 'civitai',  msg: '✓ 1/12 done · post id 9182733' },
  { t: '14:02:15.107', lvl: 'INFO', src: 'pixiv',    msg: 'Auto-mosaic check: R-18 detected → applying mask' },
  { t: '14:02:16.402', lvl: 'OK',   src: 'pixiv',    msg: '✓ Mosaic applied · 2 regions · 1.3s' },
  { t: '14:02:17.991', lvl: 'INFO', src: 'pixiv',    msg: 'Uploading 1/12 → arknights_w_01.png' },
  { t: '14:02:21.044', lvl: 'OK',   src: 'pixiv',    msg: '✓ 1/12 done · illust 121883499' },
  { t: '14:02:22.180', lvl: 'INFO', src: 'civitai',  msg: 'Uploading 2/12 → arknights_w_02.png (5.1 MB)' },
  { t: '14:02:24.715', lvl: 'WARN', src: 'civitai',  msg: 'Slow upstream · 380 KB/s · waiting' },
  { t: '14:02:31.002', lvl: 'OK',   src: 'civitai',  msg: '✓ 2/12 done · post id 9182734' },
  { t: '14:02:32.110', lvl: 'INFO', src: 'pixiv',    msg: 'Uploading 2/12 → arknights_w_02.png' },
  { t: '14:02:34.901', lvl: 'OK',   src: 'pixiv',    msg: '✓ 2/12 done · illust 121883502' },
  { t: '14:02:35.220', lvl: 'INFO', src: 'civitai',  msg: 'Uploading 3/12 → arknights_w_03.png (3.8 MB)' },
];

// ─── Layout primitives reused by MonoSingleApp ─────────────────
function MonoChannel({ label, pct, accent }) {
  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 6 }}>
        <span style={{ fontSize: 11.5, fontWeight: 600, color: M.ink, letterSpacing: '.06em', textTransform: 'uppercase' }}>{label}</span>
        <span className="mn-mono mn-num" style={{ fontSize: 12, color: accent ? M.accent : M.ink }}>{pct.toFixed(1)}%</span>
      </div>
      <div className={`mn-progress ${accent ? 'accent' : ''}`}><div style={{ width: pct + '%' }} /></div>
    </div>
  );
}

function MonoTaskRow({ t, onCancel, onRetry, onRemove }) {
  const statusMap = {
    running: { c: M.accent,   label: 'RUN',  cls: 'accent', dot: true },
    queued:  { c: M.inkFaint, label: 'WAIT', cls: 'idle' },
    done:    { c: M.ok,       label: 'DONE', cls: 'done' },
    failed:  { c: M.red,      label: 'FAIL', cls: 'fail' },
  };
  const s = statusMap[t.status] || statusMap.queued;
  const handleX = () => {
    if (t.status === 'running' || t.status === 'queued') onCancel && onCancel(t.id);
    else onRemove && onRemove(t.id);
  };
  return (
    <tr className="mn-row-hover">
      <td style={{ padding: '14px 12px', borderBottom: `1px solid ${M.lineSoft}`, width: 60 }}>
        <span className="mn-mono" style={{ fontSize: 10, color: s.c, fontWeight: 600, letterSpacing: '.08em', display: 'inline-flex', alignItems: 'center', gap: 5 }}>
          {s.dot && <span className="ms-pulse" style={{ width: 6, height: 6, borderRadius: '50%', background: s.c }} />}
          {s.label}
        </span>
      </td>
      <td style={{ padding: '14px 12px', borderBottom: `1px solid ${M.lineSoft}` }}>
        <span className="mn-mono" style={{ fontSize: 13, color: M.ink }}>{t.title}</span>
      </td>
      <td style={{ padding: '14px 12px', borderBottom: `1px solid ${M.lineSoft}`, fontSize: 12.5, color: M.inkDim }}>{t.target}</td>
      <td style={{ padding: '14px 12px', borderBottom: `1px solid ${M.lineSoft}`, width: 220 }}>
        <div className={`mn-progress ${s.cls}`}><div style={{ width: `${t.progress * 100}%` }} /></div>
      </td>
      <td style={{ padding: '14px 12px', borderBottom: `1px solid ${M.lineSoft}`, fontFamily: M.mono, fontSize: 12, color: M.ink2 }}>{t.count}</td>
      <td style={{ padding: '14px 12px', borderBottom: `1px solid ${M.lineSoft}`, fontFamily: M.mono, fontSize: 12, color: M.inkFaint, width: 70 }}>{t.eta}</td>
      <td style={{ padding: '14px 12px', borderBottom: `1px solid ${M.lineSoft}`, textAlign: 'right', width: 90 }}>
        <div style={{ display: 'flex', gap: 4, justifyContent: 'flex-end' }}>
          {t.status === 'running' && (
            <button className="mn-btn mn-btn-ghost" style={{ padding: '5px 7px' }}
                    onClick={() => onCancel && onCancel(t.id)} title="停止">
              <MIcon name="pause" size={12} />
            </button>
          )}
          {t.status === 'failed' && (
            <button className="mn-btn mn-btn-ghost" style={{ padding: '5px 7px', color: M.warn }}
                    onClick={() => onRetry && onRetry(t.id, t.cmd)} title="重试">
              <MIcon name="refresh" size={12} />
            </button>
          )}
          <button className="mn-btn mn-btn-ghost" style={{ padding: '5px 7px' }}
                  onClick={handleX} title={t.status === 'running' ? '取消' : '移除'}>
            <MIcon name="x" size={12} />
          </button>
        </div>
      </td>
    </tr>
  );
}

// ─── Stylesheet (one-time inject) ─────────────────────────────
if (typeof document !== 'undefined' && !document.getElementById('mono-styles')) {
  const s = document.createElement('style');
  s.id = 'mono-styles';
  s.textContent = `
    html, body, #root { height: 100%; margin: 0; padding: 0; }
    body { background:${M.bg2}; font-family:${M.display}; color:${M.ink}; }
    *, *::before, *::after { box-sizing: border-box; }

    .mn-mono { font-family:${M.mono}; }
    .mn-num  { font-variant-numeric: tabular-nums; }
    .mn-btn  { font-family:${M.display}; font-size:13px; font-weight:500; padding:8px 14px; border-radius:6px; border:1px solid ${M.line}; background:${M.panel}; color:${M.ink}; cursor:pointer; display:inline-flex; align-items:center; gap:8px; transition: all .12s; }
    .mn-btn:hover { background:${M.bg2}; border-color:${M.ink2}33; }
    .mn-btn-accent { background:${M.accent}; border-color:${M.accent}; color:#fff; }
    .mn-btn-accent:hover { background:#cc4a23; border-color:#cc4a23; }
    .mn-btn-ghost { background:transparent; border-color:transparent; color:${M.inkDim}; }
    .mn-btn-ghost:hover { background:${M.bg2}; color:${M.ink}; }
    .mn-input { font-family:${M.mono}; background:${M.panel}; border:1px solid ${M.line}; color:${M.ink}; padding:8px 11px; border-radius:6px; font-size:12.5px; outline:none; }
    .mn-input:focus { border-color:${M.ink}; box-shadow: 0 0 0 3px ${M.ink}10; }
    .mn-chip { display:inline-flex; align-items:center; gap:6px; font-family:${M.mono}; font-size:10.5px; padding:3px 8px; border-radius:4px; border:1px solid ${M.line}; color:${M.inkDim}; background:${M.panel}; letter-spacing:.04em; }
    .mn-progress { height:4px; background:${M.bg2}; border-radius:0; overflow:hidden; }
    .mn-progress > div { height:100%; background:${M.ink}; transition: width .4s ease; }
    .mn-progress.idle > div { background:${M.line}; }
    .mn-progress.done > div { background:${M.ok}; }
    .mn-progress.fail > div { background:${M.red}; }
    .mn-progress.accent > div { background:${M.accent}; }
    .mn-h1 { font-family:${M.display}; font-weight:700; letter-spacing:-.025em; line-height:1; }
    .mn-h2 { font-family:${M.display}; font-weight:600; letter-spacing:-.015em; }
    .mn-row-hover { transition: background .1s; }
    .mn-row-hover:hover { background:${M.bg2}; }

    .ms-section-label { font-family:${M.mono}; font-size:10px; letter-spacing:.18em; text-transform:uppercase; color:${M.inkFaint}; }
    .ms-scroll { overflow:auto; }
    .ms-scroll::-webkit-scrollbar { width:8px; height:8px; }
    .ms-scroll::-webkit-scrollbar-thumb { background:${M.line}; border-radius:4px; }
    .ms-op { padding:14px 14px; cursor:pointer; transition:background .12s; display:flex; flex-direction:column; min-height:108px; }
    .ms-op:hover { background:${M.bg2}; }
    .ms-op-key { font-family:${M.mono}; font-size:10px; color:${M.inkFaint}; letter-spacing:.12em; }
    @keyframes ms-pulse { 0%,100% { opacity:.45 } 50% { opacity:1 } }
    .ms-pulse { animation: ms-pulse 1.4s ease-in-out infinite; }

    .ms-root { font-family:${M.display}; color:${M.ink}; background:${M.bg}; height:100%; width:100%; display:flex; flex-direction:column; overflow:hidden; }
  `;
  document.head.appendChild(s);
}

Object.assign(window, { M, TIcon, MIcon, MOCK_TASKS: [], MOCK_LOG: [], MonoChannel, MonoTaskRow });
