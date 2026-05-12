// Variant C+ — Mono Single-Page
// All functionality visible on one page: hero with active job, operations
// strip, task queue, live log, settings — no tab switching.
// Designed for 1280×800 viewport. Two-column lower section keeps log + queue
// side-by-side so nothing scrolls off-screen.

// Styles for this component live in shared.jsx — keeps everything in
// one stylesheet so there's no duplicate-injection ordering trap.

function InputPromptOverlay({ prompt, task_id, onSubmit, onCancelTask }) {
  const [answer, setAnswer] = React.useState('y');
  return (
    <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.45)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 9999 }}>
      <div style={{ background: M.panel, padding: 24, borderRadius: 8, minWidth: 360, border: `1px solid ${M.line}` }}>
        <div className="mn-mono" style={{ fontSize: 13, marginBottom: 12, color: M.ink, whiteSpace: 'pre-wrap' }}>
          {prompt || '后台任务需要输入：'}
        </div>
        <input className="mn-input" value={answer} onChange={e => setAnswer(e.target.value)}
               onKeyDown={e => e.key === 'Enter' && onSubmit(answer)}
               style={{ width: '100%', marginBottom: 12 }} autoFocus />
        <div style={{ display: 'flex', gap: 8, justifyContent: 'space-between' }}>
          <button className="mn-btn mn-btn-ghost" onClick={() => onCancelTask && onCancelTask(task_id)}>取消任务</button>
          <div style={{ display: 'flex', gap: 8 }}>
            <button className="mn-btn" onClick={() => onSubmit('')}>跳过</button>
            <button className="mn-btn mn-btn-accent" onClick={() => onSubmit(answer)}>确认</button>
          </div>
        </div>
      </div>
    </div>
  );
}

function ImagePickerDialog({ cmd, onConfirm, onCancel }) {
  const [images,    setImages]    = React.useState([]);
  const [selected,  setSelected]  = React.useState(new Set());
  const [loading,   setLoading]   = React.useState(true);
  const [uploading, setUploading] = React.useState(false);
  const fileInputRef = React.useRef(null);
  const label = cmd === 2 ? 'Dual upload (Civitai + Pixiv)' : 'Pixiv only';

  const loadImages = () =>
    fetch('/api/images').then(r => r.json()).then(list => {
      setImages(list);
      setSelected(prev => {
        const existing = new Set([...prev].filter(n => list.some(f => f.name === n)));
        list.forEach(f => { if (!prev.size) existing.add(f.name); });
        return prev.size === 0 ? new Set(list.map(f => f.name)) : existing;
      });
      setLoading(false);
    }).catch(() => setLoading(false));

  React.useEffect(() => { loadImages(); }, []);

  const toggle = name => setSelected(prev => {
    const next = new Set(prev);
    if (next.has(name)) next.delete(name); else next.add(name);
    return next;
  });

  const addFiles = async e => {
    const files = Array.from(e.target.files);
    if (!files.length) return;
    setUploading(true);
    const fd = new FormData();
    files.forEach(f => fd.append('files', f));
    await fetch('/api/add-upload-files', { method: 'POST', body: fd });
    const newNames = files.map(f => f.name);
    await fetch('/api/images').then(r => r.json()).then(list => {
      setImages(list);
      setSelected(prev => {
        const next = new Set(prev);
        newNames.forEach(n => next.add(n));
        return next;
      });
    });
    setUploading(false);
    e.target.value = '';
  };

  const go = () => {
    const files = images.filter(f => selected.has(f.name)).map(f => f.name);
    onConfirm(cmd, files);
  };

  return (
    <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 9999 }}>
      <div style={{ background: M.panel, borderRadius: 8, border: `1px solid ${M.line}`, width: 640, maxHeight: '80vh', display: 'flex', flexDirection: 'column' }}>
        <div style={{ padding: '14px 18px 10px', borderBottom: `1px solid ${M.line}` }}>
          <div style={{ fontSize: 13.5, fontWeight: 600, marginBottom: 2 }}>{label}</div>
          <div className="mn-mono" style={{ fontSize: 11, color: M.inkDim }}>
            {loading ? '加载中…' : `upload/ 共 ${images.length} 张 · 已选 ${selected.size} 张`}
          </div>
        </div>
        <div style={{ padding: '8px 18px', borderBottom: `1px solid ${M.lineSoft}`, display: 'flex', gap: 8, alignItems: 'center' }}>
          <button className="mn-btn mn-btn-ghost" style={{ fontSize: 12 }}
                  onClick={() => setSelected(new Set(images.map(f => f.name)))}>全选</button>
          <button className="mn-btn mn-btn-ghost" style={{ fontSize: 12 }}
                  onClick={() => setSelected(new Set())}>清空</button>
          <div style={{ marginLeft: 'auto' }}>
            <input ref={fileInputRef} type="file" multiple accept="image/*" style={{ display: 'none' }} onChange={addFiles} />
            <button className="mn-btn mn-btn-ghost" style={{ fontSize: 12 }}
                    onClick={() => fileInputRef.current.click()} disabled={uploading}>
              <MIcon name="plus" size={12} /> {uploading ? '导入中…' : '添加文件'}
            </button>
          </div>
        </div>
        <div style={{ flex: 1, overflow: 'auto', padding: 14 }}>
          {loading && (
            <div style={{ textAlign: 'center', color: M.inkFaint, padding: 24, fontFamily: M.mono, fontSize: 12 }}>加载中…</div>
          )}
          {!loading && images.length === 0 && (
            <div style={{ textAlign: 'center', color: M.inkFaint, padding: 24, fontFamily: M.mono, fontSize: 12 }}>
              upload/ 为空。点"添加文件"从电脑上选图。
            </div>
          )}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(110px, 1fr))', gap: 10 }}>
            {images.map(f => {
              const sel = selected.has(f.name);
              return (
                <div key={f.name} onClick={() => toggle(f.name)}
                     style={{ cursor: 'pointer', borderRadius: 6, border: `2px solid ${sel ? M.accent : M.line}`, overflow: 'hidden', position: 'relative', background: M.bg }}>
                  <img src={`/upload/${encodeURIComponent(f.name)}`} alt={f.name}
                       style={{ width: '100%', aspectRatio: '1', objectFit: 'cover', display: 'block' }} />
                  <div style={{ position: 'absolute', top: 4, right: 4, width: 18, height: 18, borderRadius: '50%',
                                background: sel ? M.accent : 'rgba(0,0,0,0.45)', display: 'grid', placeItems: 'center' }}>
                    {sel && <span style={{ color: '#fff', fontSize: 11, lineHeight: 1 }}>✓</span>}
                  </div>
                  <div style={{ padding: '3px 5px', fontSize: 10, fontFamily: M.mono, color: M.inkFaint,
                                whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', background: M.panel }}>
                    {f.name}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
        <div style={{ padding: '10px 18px 14px', borderTop: `1px solid ${M.line}`, display: 'flex', gap: 8, alignItems: 'center' }}>
          <button className="mn-btn mn-btn-ghost" style={{ fontSize: 12, marginRight: 'auto' }}
                  onClick={() => onConfirm(cmd, [])} title="随机从 upload/ 选 1-5 张，和命令行行为一致">
            随机 1-5
          </button>
          <button className="mn-btn" onClick={onCancel}>取消</button>
          <button className="mn-btn mn-btn-accent" onClick={go}
                  disabled={selected.size === 0} style={{ opacity: selected.size === 0 ? 0.5 : 1 }}>
            上传 {selected.size} 张
          </button>
        </div>
      </div>
    </div>
  );
}

function TaggerSetupDialog({ onClose }) {
  const [haintag,   setHaintag]   = React.useState('');
  const [modelDir,  setModelDir]  = React.useState('');
  const [haintagOk, setHaintagOk] = React.useState(null);
  const [modelOk,   setModelOk]   = React.useState(null);
  const [saving,    setSaving]    = React.useState(false);
  const [saved,     setSaved]     = React.useState(false);

  React.useEffect(() => {
    fetch('/api/tagger-config').then(r => r.json()).then(d => {
      setHaintag(d.haintag_root || '');
      setModelDir(d.model_dir || '');
      setHaintagOk(d.haintag_ok);
      setModelOk(d.model_ok);
    }).catch(() => {});
  }, []);

  // POST current inputs → server saves + checks paths → update ok indicators
  const postAndVerify = (closeAfter) => {
    setSaving(true);
    fetch('/api/tagger-config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ haintag_root: haintag, model_dir: modelDir }),
    })
      .then(r => r.json())
      .then(() => fetch('/api/tagger-config'))
      .then(r => r.json())
      .then(d => {
        setSaving(false);
        setHaintagOk(d.haintag_ok);
        setModelOk(d.model_ok);
        if (closeAfter) {
          setSaved(true);
          setTimeout(() => { setSaved(false); onClose(true); }, 900);
        }
      })
      .catch(() => setSaving(false));
  };

  const dismiss = () => {
    localStorage.setItem('tagger-setup-dismissed', '1');
    onClose(false);
  };

  return (
    <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 9999 }}>
      <div style={{ background: M.panel, borderRadius: 8, border: `1px solid ${M.line}`, width: 540, padding: '20px 24px' }}>
        <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>Tagger setup (WD14)</div>
        <div className="mn-mono" style={{ fontSize: 11, color: M.inkDim, marginBottom: 18 }}>
          Both fields are optional — uploads still work without them.
        </div>

        {/* haintag root */}
        <div style={{ marginBottom: 16 }}>
          <div style={{ display: 'flex', alignItems: 'center', marginBottom: 5 }}>
            <span style={{ fontSize: 12.5 }}>haintag root</span>
            <span className="mn-mono" style={{ marginLeft: 6, fontSize: 10.5, color: M.inkFaint }}>optional</span>
            {haintagOk !== null && (
              <span style={{ marginLeft: 'auto', fontSize: 11, color: haintagOk ? M.ok : M.red }}>
                {haintagOk ? '✓ native_app/tagger.py found' : '✗ native_app/tagger.py not found'}
              </span>
            )}
          </div>
          <input className="mn-input" value={haintag} onChange={e => setHaintag(e.target.value)}
                 placeholder="e.g. E:\projects\haintag" style={{ width: '100%', fontSize: 12 }} />
          <div className="mn-mono" style={{ fontSize: 10.5, color: M.inkFaint, marginTop: 4, lineHeight: 1.6 }}>
            The folder that contains <span style={{ color: M.ink2 }}>native_app/tagger.py</span> (haintag repo root).<br />
            Uses TaggerEngine subprocess mode — haintag's own venv handles onnxruntime.<br />
            Leave empty → standalone mode (needs onnxruntime in current env).
          </div>
        </div>

        {/* model directory */}
        <div style={{ marginBottom: 20 }}>
          <div style={{ display: 'flex', alignItems: 'center', marginBottom: 5 }}>
            <span style={{ fontSize: 12.5 }}>model directory</span>
            {modelOk !== null && (
              <span style={{ marginLeft: 'auto', fontSize: 11, color: modelOk ? M.ok : M.red }}>
                {modelOk ? '✓ .onnx + mapping found' : modelDir ? '✗ .onnx or mapping not found' : '—'}
              </span>
            )}
          </div>
          <input className="mn-input" value={modelDir} onChange={e => setModelDir(e.target.value)}
                 placeholder="e.g. E:\ComfyUI\models\onnx\cl_tagger" style={{ width: '100%', fontSize: 12 }} />
          <div className="mn-mono" style={{ fontSize: 10.5, color: M.inkFaint, marginTop: 4, lineHeight: 1.6 }}>
            Must contain: <span style={{ color: M.ink2 }}>*.onnx</span> (model file, e.g. <span style={{ color: M.ink2 }}>cl_tagger_1_02.onnx</span>)<br />
            + <span style={{ color: M.ink2 }}>*tag*mapping*.json</span> or <span style={{ color: M.ink2 }}>*label*.json</span> or <span style={{ color: M.ink2 }}>*tag*.csv</span> (tag list).<br />
            ComfyUI default: <span style={{ color: M.ink2 }}>ComfyUI\models\onnx\cl_tagger\</span>
          </div>
        </div>

        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button className="mn-btn mn-btn-ghost" onClick={dismiss} style={{ fontSize: 12 }}>Skip</button>
          <button className="mn-btn" onClick={() => postAndVerify(false)} disabled={saving} style={{ fontSize: 12 }}>
            {saving ? '…' : 'Verify'}
          </button>
          <button className="mn-btn mn-btn-accent" onClick={() => postAndVerify(true)} disabled={saving} style={{ fontSize: 12 }}>
            {saved ? '✓ Saved' : saving ? '…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  );
}

function SchedulerDialog({ current, onClose, onSave }) {
  const sched = current || {};
  const [minHours, setMinHours] = React.useState(String(sched.min_hours ?? 1));
  const [maxHours, setMaxHours] = React.useState(String(sched.max_hours ?? 3));
  const [count,    setCount]    = React.useState(String(sched.count ?? 1));
  const [civitai,  setCivitai]  = React.useState((sched.targets || 'civitai,pixiv').includes('civitai'));
  const [pixiv,    setPixiv]    = React.useState((sched.targets || 'civitai,pixiv').includes('pixiv'));
  const [saving,   setSaving]   = React.useState(false);
  const [err,      setErr]      = React.useState('');

  const submit = () => {
    const min = parseFloat(minHours), max = parseFloat(maxHours), cnt = parseInt(count, 10);
    if (!min || !max || min <= 0 || max <= 0 || min > max) { setErr('时间范围无效（min 需 ≤ max）'); return; }
    if (!cnt || cnt < 1) { setErr('张数至少 1'); return; }
    const targets = [civitai && 'civitai', pixiv && 'pixiv'].filter(Boolean).join(',');
    if (!targets) { setErr('至少选一个目标'); return; }
    setSaving(true); setErr('');
    fetch('/api/scheduler', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: true, min_hours: min, max_hours: max, count: cnt, targets }),
    })
      .then(r => r.json())
      .then(d => { setSaving(false); if (d.ok) { onSave(); onClose(); } else { setErr(d.error || '保存失败'); } })
      .catch(() => { setSaving(false); setErr('请求失败'); });
  };

  return (
    <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 9999 }}>
      <div style={{ background: M.panel, borderRadius: 8, border: `1px solid ${M.line}`, width: 380, padding: '20px 24px' }}>
        <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>Auto schedule</div>
        <div className="mn-mono" style={{ fontSize: 11, color: M.inkDim, marginBottom: 16 }}>定时自动发布 — 每隔随机间隔触发一次上传</div>

        <div style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 12.5, marginBottom: 6 }}>发布间隔（小时）</div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <input className="mn-input" value={minHours} onChange={e => setMinHours(e.target.value)} style={{ width: 72, fontSize: 12 }} placeholder="min" />
            <span className="mn-mono" style={{ fontSize: 11, color: M.inkDim }}>~</span>
            <input className="mn-input" value={maxHours} onChange={e => setMaxHours(e.target.value)} style={{ width: 72, fontSize: 12 }} placeholder="max" />
            <span className="mn-mono" style={{ fontSize: 11, color: M.inkDim }}>h (随机)</span>
          </div>
        </div>

        <div style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 12.5, marginBottom: 6 }}>每次发布张数</div>
          <input className="mn-input" value={count} onChange={e => setCount(e.target.value)} style={{ width: 72, fontSize: 12 }} />
        </div>

        <div style={{ marginBottom: 18 }}>
          <div style={{ fontSize: 12.5, marginBottom: 6 }}>发布目标</div>
          <div style={{ display: 'flex', gap: 18 }}>
            <label style={{ display: 'flex', gap: 6, alignItems: 'center', fontSize: 12.5, cursor: 'pointer' }}>
              <input type="checkbox" checked={civitai} onChange={e => setCivitai(e.target.checked)} /> Civitai
            </label>
            <label style={{ display: 'flex', gap: 6, alignItems: 'center', fontSize: 12.5, cursor: 'pointer' }}>
              <input type="checkbox" checked={pixiv} onChange={e => setPixiv(e.target.checked)} /> Pixiv
            </label>
          </div>
        </div>

        {err && <div className="mn-mono" style={{ fontSize: 11, color: M.red, marginBottom: 10 }}>{err}</div>}

        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button className="mn-btn mn-btn-ghost" onClick={onClose} style={{ fontSize: 12 }}>取消</button>
          <button className="mn-btn mn-btn-accent" onClick={submit} disabled={saving} style={{ fontSize: 12 }}>{saving ? '…' : '启用'}</button>
        </div>
      </div>
    </div>
  );
}

function MonoSingleApp() {
  const [filter, setFilter] = React.useState('all');
  const [tick,   setTick]   = React.useState(0);
  const [tasks,  setTasks]  = React.useState([]);
  const [logs,   setLogs]   = React.useState([]);
  const [connected,      setConnected]      = React.useState(false);
  const [pendingInput,   setPendingInput]   = React.useState(null);
  const [uploadDialog,   setUploadDialog]   = React.useState(null);
  const [taggerSetup,    setTaggerSetup]    = React.useState(false);
  const [taggerConfigured, setTaggerConfigured] = React.useState(true);
  const [schedulerDialog, setSchedulerDialog] = React.useState(false);
  const [status, setStatus] = React.useState({ mosaic_installed: false, upload_count: 0, has_api_key: false, pixiv_logged_in: false, civitai_logged_in: false, scheduler: { enabled: false, next_fire_at: null, min_hours: 1, max_hours: 3, count: 1, targets: 'civitai,pixiv' } });
  const [isDark, setIsDark] = React.useState(() => localStorage.getItem('mn-theme') === 'dark');

  React.useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), 800);
    return () => clearInterval(id);
  }, []);

  React.useEffect(() => {
    fetch('/api/status').then(r => r.json()).then(setStatus).catch(() => {});
  }, []);

  React.useEffect(() => {
    fetch('/api/tagger-config').then(r => r.json()).then(d => {
      setTaggerConfigured(d.model_ok || false);
      if (d.needs_setup && !localStorage.getItem('tagger-setup-dismissed')) {
        setTaggerSetup(true);
      }
    }).catch(() => {});
  }, []);

  React.useEffect(() => {
    const t = isDark ? _DARK : _LIGHT;
    Object.assign(M, t);
    updateCssVars(M);
    localStorage.setItem('mn-theme', isDark ? 'dark' : 'light');
  }, [isDark]);

  React.useEffect(() => {
    let unloading = false;
    const notifyShutdown = () => {
      if (unloading) return;
      unloading = true;
      navigator.sendBeacon('/api/shutdown', new Blob(['{}'], { type: 'application/json' }));
    };
    window.addEventListener('pagehide', notifyShutdown);
    window.addEventListener('beforeunload', notifyShutdown);
    const es = new EventSource('/api/stream');
    es.addEventListener('task_update', e => {
      const t = JSON.parse(e.data);
      setTasks(prev => {
        const idx = prev.findIndex(x => x.id === t.id);
        if (idx >= 0) { const n = [...prev]; n[idx] = t; return n; }
        return [...prev, t];
      });
    });
    es.addEventListener('log', e => {
      setLogs(prev => [...prev.slice(-499), JSON.parse(e.data)]);
    });
    es.addEventListener('scheduler_update', e => {
      const scheduler = JSON.parse(e.data);
      setStatus(prev => ({ ...prev, scheduler }));
    });
    es.addEventListener('task_remove', e => {
      const { id } = JSON.parse(e.data);
      setTasks(prev => prev.filter(t => t.id !== id));
    });
    es.addEventListener('input_required', e => setPendingInput(JSON.parse(e.data)));
    let errTimer = null;
    es.onopen  = () => { clearTimeout(errTimer); setConnected(true); };
    es.onerror = () => { errTimer = setTimeout(() => { if (es.readyState !== 1) setConnected(false); }, 2000); };
    return () => {
      window.removeEventListener('pagehide', notifyShutdown);
      window.removeEventListener('beforeunload', notifyShutdown);
      es.close();
    };
  }, []);

  const runCmd = (cmd, params = {}) =>
    fetch(`/api/run/${cmd}`, { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(params) });

  const replyInput = (taskId, answer) => {
    fetch(`/api/tasks/${taskId}/resume`, { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ answer }) });
    setPendingInput(null);
  };

  const cancelTask = id =>
    fetch(`/api/tasks/${id}/cancel`, { method: 'POST' });

  const removeTask = id => {
    fetch(`/api/tasks/${id}/remove`, { method: 'POST' });
    setTasks(prev => prev.filter(t => t.id !== id));
  };

  const retryTask = (id, cmd) => {
    removeTask(id);
    runCmd(cmd);
  };

  const reloadStatus = () =>
    fetch('/api/status').then(r => r.json()).then(setStatus).catch(() => {});

  const startUpload = cmd => setUploadDialog({ cmd });
  const confirmUpload = (cmd, files) => {
    setUploadDialog(null);
    runCmd(cmd, files && files.length > 0 ? { files } : {});
  };

  const runningCount = tasks.filter(t => t.status === 'running').length;

  return (
    <div className="ms-root">
      {pendingInput && (
        <InputPromptOverlay
          {...pendingInput}
          onSubmit={ans => replyInput(pendingInput.task_id, ans)}
          onCancelTask={cancelTask}
        />
      )}
      {uploadDialog && (
        <ImagePickerDialog
          cmd={uploadDialog.cmd}
          onConfirm={confirmUpload}
          onCancel={() => setUploadDialog(null)}
        />
      )}
      {taggerSetup && (
        <TaggerSetupDialog onClose={saved => {
          setTaggerSetup(false);
          if (saved) fetch('/api/tagger-config').then(r => r.json()).then(d => setTaggerConfigured(d.model_ok || false)).catch(() => {});
        }} />
      )}
      {schedulerDialog && (
        <SchedulerDialog
          current={status.scheduler}
          onClose={() => setSchedulerDialog(false)}
          onSave={() => fetch('/api/status').then(r => r.json()).then(setStatus).catch(() => {})}
        />
      )}

      {/* ── Top bar ─────────────────────────────────────────────── */}
      <header style={{ padding: '14px 24px', borderBottom: `1px solid ${M.line}`, display: 'flex', alignItems: 'center', gap: 18, background: M.panel, flexShrink: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <div style={{ width: 28, height: 28, borderRadius: 5, background: M.ink, display: 'grid', placeItems: 'center' }}>
            <span className="mn-mono" style={{ fontSize: 11, fontWeight: 700, color: '#fff' }}>cp</span>
          </div>
          <div style={{ lineHeight: 1.15 }}>
            <div style={{ fontSize: 13.5, fontWeight: 600 }}>Civitai · Pixiv Uploader</div>
            <div className="mn-mono" style={{ fontSize: 10, color: M.inkFaint }}>single-page console</div>
          </div>
        </div>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 10, alignItems: 'center' }}>
          <span className="mn-chip">
            <span style={{ width: 6, height: 6, borderRadius: '50%', background: status.mosaic_installed ? M.ok : M.inkFaint }} />
            R-18 mosaic {status.mosaic_installed ? 'ON' : 'OFF'}
          </span>
          <span className="mn-chip">upload/ {status.upload_count} imgs</span>
          <button className="mn-btn mn-btn-ghost" style={{ padding: '5px 8px' }}
                  onClick={() => setIsDark(d => !d)} title={isDark ? '切换日间模式' : '切换夜间模式'}>
            <MIcon name={isDark ? 'sun' : 'moon'} size={14} />
          </button>
        </div>
      </header>

      {/* ── Body: 4-zone layout ────────────────────────────────── */}
      <div style={{ flex: 1, display: 'grid', gridTemplateColumns: '1fr 380px', gap: 0, minHeight: 0 }}>
        {/* LEFT column: hero + operations + queue */}
        <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0, borderRight: `1px solid ${M.line}` }}>
          <ActiveHero tick={tick} tasks={tasks} />
          <OperationsStrip runCmd={runCmd} onStartUpload={startUpload} />
          <QueueZone filter={filter} setFilter={setFilter} tasks={tasks}
                     onCancel={cancelTask} onRemove={removeTask} onRetry={retryTask} />
        </div>

        {/* RIGHT column: log + settings */}
        <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0 }}>
          <LogZone logs={logs} />
          <SettingsZone status={status} onStatusReload={reloadStatus}
                       taggerConfigured={taggerConfigured}
                       onTaggerSetup={() => setTaggerSetup(true)}
                       tick={tick}
                       onSchedulerConfigure={() => setSchedulerDialog(true)} />
        </div>
      </div>

      {/* ── Status bar ─────────────────────────────────────────── */}
      <div style={{ height: 26, borderTop: `1px solid ${M.line}`, background: M.panel, display: 'flex', alignItems: 'center', padding: '0 18px', fontSize: 11, fontFamily: M.mono, color: M.inkFaint, gap: 16, flexShrink: 0 }}>
        <span style={{ color: connected ? M.ok : M.red }}>● {connected ? 'connected' : 'disconnected'}</span>
        <span style={{ marginLeft: 'auto', color: runningCount > 0 ? M.accent : M.inkFaint, fontWeight: runningCount > 0 ? 600 : 400 }}>
          {runningCount} {runningCount === 1 ? 'job' : 'jobs'} running
        </span>
      </div>
    </div>
  );
}

// ── ZONE 1: Active job hero ────────────────────────────────────
function ActiveHero({ tick, tasks }) {
  const active = tasks.find(t => t.status === 'running');
  const civPct = active ? active.progress * 100 : 0;
  const pxvPct = active ? Math.max(0, active.progress * 100 - 5 + (tick % 4) * 0.3) : 0;

  if (!active) {
    return (
      <div style={{ padding: '20px 24px 18px', borderBottom: `1px solid ${M.line}`, background: M.panel }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
          <span className="ms-section-label">active job</span>
        </div>
        <div style={{ fontSize: 14, color: M.inkDim }}>No job running. Use the operations below to start one.</div>
      </div>
    );
  }

  return (
    <div style={{ padding: '20px 24px 18px', borderBottom: `1px solid ${M.line}`, background: M.panel }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 24 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
            <span className="mn-mono ms-pulse" style={{ fontSize: 10, color: M.accent, fontWeight: 600, letterSpacing: '.12em', display: 'inline-flex', alignItems: 'center', gap: 5 }}>
              <span style={{ width: 6, height: 6, borderRadius: '50%', background: M.accent }} /> LIVE
            </span>
            <span className="ms-section-label">active job</span>
          </div>
          <div className="mn-h1" style={{ fontSize: 28, marginBottom: 4 }}>{active.title}</div>
          <div style={{ fontSize: 13, color: M.inkDim, marginBottom: 14 }}>
            {active.target} · {active.count} · ETA {active.eta}
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 18 }}>
            <MonoChannel label="Civitai" pct={civPct} accent />
            <MonoChannel label="Pixiv"   pct={pxvPct} />
          </div>
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, flexShrink: 0, paddingLeft: 18, borderLeft: `1px solid ${M.line}` }}>
          <button className="mn-btn" onClick={() => fetch(`/api/tasks/${active.id}/cancel`, { method: 'POST' })}>
            <MIcon name="stop" size={12} /> Cancel
          </button>
          <button className="mn-btn mn-btn-ghost" onClick={() => fetch('/api/open-folder')}>
            <MIcon name="folder" size={13} /> Folder
          </button>
        </div>
      </div>
    </div>
  );
}

// ── ZONE 2: Operations strip — all 5 actions visible ───────────
function OperationsStrip({ runCmd, onStartUpload }) {
  const ops = [
    { key: '1', icon: 'split',   title: 'Split post',     sub: '一帖多图 → 多帖单图', cmd: 1 },
    { key: '2', icon: 'upload',  title: 'Dual upload',    sub: 'Civitai + Pixiv',     cmd: 2, upload: true },
    { key: '3', icon: 'image',   title: 'Pixiv only',     sub: '跳过 Civitai',        cmd: 3, upload: true },
    { key: '4', icon: 'shield',  title: 'R-18 mosaic',    sub: '安装 / 检查',         cmd: 4 },
    { key: '5', icon: 'refresh', title: 'Update',         sub: '检查更新',            cmd: 5 },
  ];
  return (
    <div style={{ borderBottom: `1px solid ${M.line}`, background: M.bg }}>
      <div style={{ padding: '12px 24px 8px', display: 'flex', alignItems: 'baseline', justifyContent: 'space-between' }}>
        <span className="ms-section-label">operations</span>
        <span className="mn-mono" style={{ fontSize: 10, color: M.inkFaint }}>press 1–5 · or click</span>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', borderTop: `1px solid ${M.line}` }}>
        {ops.map((o, i) => (
          <div key={o.key} className="ms-op"
               onClick={() => o.upload ? onStartUpload(o.cmd) : runCmd(o.cmd)}
               style={{ borderRight: i < ops.length - 1 ? `1px solid ${M.line}` : 'none' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <span className="ms-op-key">[{o.key}]</span>
              <MIcon name={o.icon} size={16} color={M.ink2} />
            </div>
            <div style={{ marginTop: 'auto' }}>
              <div className="mn-h2" style={{ fontSize: 14, marginBottom: 2 }}>{o.title}</div>
              <div style={{ fontSize: 11.5, color: M.inkDim, lineHeight: 1.4 }}>{o.sub}</div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── ZONE 3: Queue ──────────────────────────────────────────────
function QueueZone({ filter, setFilter, tasks, onCancel, onRemove, onRetry }) {
  const running  = tasks.filter(t => t.status === 'running').length;
  const queued   = tasks.filter(t => t.status === 'queued').length;
  const done     = tasks.filter(t => t.status === 'done').length;
  const failed   = tasks.filter(t => t.status === 'failed').length;
  const canceled = tasks.filter(t => t.status === 'canceled').length;
  const filters = [
    { id: 'all',      label: 'All',      n: tasks.length },
    { id: 'running',  label: 'Running',  n: running },
    { id: 'queued',   label: 'Queued',   n: queued  },
    { id: 'done',     label: 'Done',     n: done    },
    { id: 'failed',   label: 'Failed',   n: failed  },
    { id: 'canceled', label: 'Canceled', n: canceled },
  ];
  const list = filter === 'all' ? tasks : tasks.filter(t => t.status === filter);

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0, background: M.panel }}>
      <div style={{ padding: '10px 24px 0', display: 'flex', alignItems: 'baseline', justifyContent: 'space-between' }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 14 }}>
          <span className="ms-section-label">queue</span>
          <div style={{ display: 'flex', gap: 0 }}>
            {filters.map(f => {
              const active = filter === f.id;
              return (
                <button key={f.id} onClick={() => setFilter(f.id)}
                  className="mn-btn mn-btn-ghost"
                  style={{
                    borderRadius: 0, padding: '6px 10px', fontSize: 12,
                    color: active ? M.ink : M.inkDim,
                    fontWeight: active ? 600 : 500,
                    boxShadow: active ? `inset 0 -2px 0 ${M.accent}` : 'none',
                    background: 'transparent',
                  }}>
                  {f.label} <span className="mn-mono mn-num" style={{ fontSize: 10, color: M.inkFaint, marginLeft: 3 }}>{f.n}</span>
                </button>
              );
            })}
          </div>
        </div>
        <div style={{ display: 'flex', gap: 4 }}>
          <button className="mn-btn mn-btn-ghost" style={{ padding: '4px 8px', fontSize: 12 }}
                  onClick={() => window.location.reload()} title="刷新页面">
            <MIcon name="refresh" size={12} />
          </button>
        </div>
      </div>

      <div className="ms-scroll" style={{ flex: 1, padding: '4px 0 8px' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              {[
                { h: '', w: 56 },
                { h: 'Task' },
                { h: 'Target', w: 110 },
                { h: 'Progress', w: 140 },
                { h: 'Imgs', w: 70 },
                { h: 'ETA', w: 56 },
                { h: '', w: 88 },
              ].map((c, i) => (
                <th key={i} style={{ width: c.w, textAlign: 'left', fontSize: 9.5, fontFamily: M.mono, fontWeight: 500, color: M.inkFaint, letterSpacing: '.1em', textTransform: 'uppercase', padding: '8px 12px', borderBottom: `1px solid ${M.line}` }}>
                  {c.h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {list.map(t => <MonoTaskRow key={t.id} t={t} onCancel={onCancel} onRemove={onRemove} onRetry={onRetry} />)}
          </tbody>
        </table>
        {list.length === 0 && (
          <div style={{ padding: '24px', textAlign: 'center', color: M.inkFaint, fontFamily: M.mono, fontSize: 12 }}>
            No tasks yet. Click an operation above to start.
          </div>
        )}
      </div>
    </div>
  );
}

// ── ZONE 4: Log (right column top) ─────────────────────────────
function LogZone({ logs }) {
  const [grep, setGrep]   = React.useState('');
  const [paused, setPaused] = React.useState(false);
  const endRef = React.useRef(null);
  const lvlColor = { INFO: M.ink, OK: M.ok, WARN: M.warn, ERR: M.red };

  React.useEffect(() => {
    if (!paused && endRef.current) {
      endRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [logs, paused]);

  const filtered = grep
    ? logs.filter(l => l.msg.toLowerCase().includes(grep.toLowerCase()) || l.src.toLowerCase().includes(grep.toLowerCase()))
    : logs;

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0, background: M.panel, borderBottom: `1px solid ${M.line}` }}>
      <div style={{ padding: '10px 18px 8px', display: 'flex', alignItems: 'center', gap: 10, borderBottom: `1px solid ${M.lineSoft}` }}>
        <span className="ms-section-label">log</span>
        <span className="mn-chip" style={{ fontSize: 9.5 }}>
          <span className="ms-pulse" style={{ width: 5, height: 5, borderRadius: '50%', background: M.accent }} /> live
        </span>
        <input className="mn-input" placeholder="grep" value={grep} onChange={e => setGrep(e.target.value)}
               style={{ flex: 1, padding: '4px 8px', fontSize: 11.5 }} />
        <button className="mn-btn mn-btn-ghost" style={{ padding: '4px 6px' }}
                onClick={() => setPaused(p => !p)}>
          <MIcon name={paused ? 'play' : 'pause'} size={12} />
        </button>
      </div>
      <div className="ms-scroll" style={{ flex: 1, padding: '6px 0', background: M.panel, fontFamily: M.mono, fontSize: 11, lineHeight: 1.55 }}>
        {filtered.map((l, i) => (
          <div key={i} className="mn-row-hover" style={{ padding: '1px 14px', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', wordBreak: 'break-word' }}>
            <span style={{ color: M.inkFaint }}>{l.t.slice(3, 12)}</span>
            {' '}<span style={{ color: lvlColor[l.lvl] || M.ink, fontWeight: 600 }}>{(l.lvl || 'INFO').padEnd(4)}</span>
            {' '}<span style={{ color: M.accent }}>{l.src}</span>
            {' '}<span style={{ color: M.ink2 }}>{l.msg}</span>
          </div>
        ))}
        <div ref={endRef} style={{ padding: '6px 14px', color: M.accent }}>
          {'> '}<span className="ms-pulse">▮</span>
        </div>
      </div>
    </div>
  );
}

// ── Settings (right column bottom) ─────────────────────────────
function SettingsZone({ status, onStatusReload, taggerConfigured, onTaggerSetup, tick, onSchedulerConfigure }) {
  const [apiKey,        setApiKey]        = React.useState('');
  const [saved,         setSaved]         = React.useState(false);
  const [pixivSwitching,   setPixivSwitching]   = React.useState(false);
  const [pixivMsg,         setPixivMsg]         = React.useState('');
  const [civitaiSwitching, setCivitaiSwitching] = React.useState(false);
  const [civitaiMsg,       setCivitaiMsg]       = React.useState('');

  const fmtNextFire = iso => {
    if (!iso) return '—';
    const diff = Math.floor((new Date(iso) - Date.now()) / 1000);
    if (diff <= 0) return 'soon';
    const h = Math.floor(diff / 3600), m = Math.floor((diff % 3600) / 60);
    return h > 0 ? `${h}h ${m}m` : `${m}m`;
  };
  const sched = status.scheduler || { enabled: false, next_fire_at: null };

  React.useEffect(() => {
    if (!sched.enabled) return;
    const fireAt = sched.next_fire_at ? new Date(sched.next_fire_at).getTime() : 0;
    const delay = fireAt > Date.now() ? fireAt - Date.now() + 1500 : 1500;
    const id = setTimeout(() => onStatusReload && onStatusReload(), delay);
    return () => clearTimeout(id);
  }, [sched.enabled, sched.next_fire_at]);

  const saveKey = () => {
    if (!apiKey.trim()) return;
    fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key: apiKey.trim() }),
    }).then(r => {
      if (!r.ok) { alert('保存失败（' + r.status + '），请重启服务器后重试'); return; }
      setSaved(true);
      setApiKey('');
      setTimeout(() => { setSaved(false); onStatusReload && onStatusReload(); }, 1200);
    });
  };

  return (
    <div style={{ background: M.panel, padding: '12px 18px 14px', flexShrink: 0 }}>
      <div className="ms-section-label" style={{ marginBottom: 8 }}>settings</div>
      <SetCompactRow label="Mosaic model" value={status.mosaic_installed ? 'installed' : 'not installed'} ok={status.mosaic_installed} />
      <div style={{ display: 'flex', alignItems: 'center', padding: '7px 0', borderBottom: `1px solid ${M.lineSoft}` }}>
        <div style={{ fontSize: 12.5 }}>WD14 tagger</div>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
          <span style={{ width: 6, height: 6, borderRadius: '50%', background: taggerConfigured ? M.ok : M.red }} />
          <span className="mn-mono" style={{ fontSize: 11.5, color: M.inkDim }}>{taggerConfigured ? 'configured' : 'not set'}</span>
          <button className="mn-btn mn-btn-ghost" onClick={onTaggerSetup} style={{ padding: '2px 8px', fontSize: 11 }}>Configure</button>
        </div>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', padding: '7px 0', borderBottom: `1px solid ${M.lineSoft}` }}>
        <div style={{ fontSize: 12.5 }}>Pixiv account</div>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
          {pixivMsg && <span className="mn-mono" style={{ fontSize: 10.5, color: M.red }}>{pixivMsg}</span>}
          <span style={{ width: 6, height: 6, borderRadius: '50%', background: status.pixiv_logged_in ? M.ok : M.inkFaint }} />
          <span className="mn-mono" style={{ fontSize: 11.5, color: M.inkDim }}>{status.pixiv_logged_in ? 'logged in' : 'not set'}</span>
          <button className="mn-btn mn-btn-ghost"
                  disabled={pixivSwitching}
                  onClick={() => {
                    setPixivSwitching(true);
                    setPixivMsg('');
                    fetch('/api/pixiv-logout', { method: 'POST' })
                      .then(r => r.json().then(d => ({ ok: r.ok, d })))
                      .then(({ ok, d }) => {
                        setPixivSwitching(false);
                        if (ok) { onStatusReload && onStatusReload(); }
                        else { setPixivMsg(d.error === 'pixiv task is running' ? '停止当前任务后再切换' : d.error); }
                      })
                      .catch(() => { setPixivSwitching(false); setPixivMsg('请求失败'); });
                  }}
                  style={{ padding: '2px 8px', fontSize: 11 }}>
            {pixivSwitching ? '…' : 'Switch account'}
          </button>
        </div>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', padding: '7px 0', borderBottom: `1px solid ${M.lineSoft}` }}>
        <div style={{ fontSize: 12.5 }}>Civitai account</div>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
          {civitaiMsg && <span className="mn-mono" style={{ fontSize: 10.5, color: M.red }}>{civitaiMsg}</span>}
          <span style={{ width: 6, height: 6, borderRadius: '50%', background: status.civitai_logged_in ? M.ok : M.inkFaint }} />
          <span className="mn-mono" style={{ fontSize: 11.5, color: M.inkDim }}>{status.civitai_logged_in ? 'logged in' : 'not set'}</span>
          <button className="mn-btn mn-btn-ghost"
                  disabled={civitaiSwitching}
                  onClick={() => {
                    setCivitaiSwitching(true);
                    setCivitaiMsg('');
                    fetch('/api/civitai-logout', { method: 'POST' })
                      .then(r => r.json().then(d => ({ ok: r.ok, d })))
                      .then(({ ok, d }) => {
                        setCivitaiSwitching(false);
                        if (ok) { onStatusReload && onStatusReload(); }
                        else { setCivitaiMsg(d.error === 'civitai task is running' ? '停止当前任务后再切换' : d.error); }
                      })
                      .catch(() => { setCivitaiSwitching(false); setCivitaiMsg('请求失败'); });
                  }}
                  style={{ padding: '2px 8px', fontSize: 11 }}>
            {civitaiSwitching ? '…' : 'Switch account'}
          </button>
        </div>
      </div>
      <SetCompactRow label="Upload queue" value={`${status.upload_count} imgs`} />
      <div style={{ display: 'flex', alignItems: 'center', padding: '7px 0', borderBottom: `1px solid ${M.lineSoft}` }}>
        <div style={{ fontSize: 12.5 }}>Auto schedule</div>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
          <span className="mn-mono" style={{ fontSize: 11.5, color: sched.enabled ? M.ok : M.inkDim }}>
            {sched.enabled ? `Next: in ${fmtNextFire(sched.next_fire_at)}` : 'off'}
          </span>
          {sched.enabled && (
            <button className="mn-btn mn-btn-ghost"
                    onClick={() => fetch('/api/scheduler', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: false }) })
                      .then(() => onStatusReload && onStatusReload())}
                    style={{ padding: '2px 8px', fontSize: 11 }}>
              Disable
            </button>
          )}
          <button className="mn-btn mn-btn-ghost" onClick={onSchedulerConfigure} style={{ padding: '2px 8px', fontSize: 11 }}>Configure</button>
        </div>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', padding: '7px 0', borderBottom: `1px solid ${M.lineSoft}` }}>
        <div style={{ fontSize: 12.5 }}>API key</div>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 6, alignItems: 'center' }}>
          <span style={{ width: 6, height: 6, borderRadius: '50%', background: status.has_api_key ? M.ok : M.red }} />
          <span className="mn-mono" style={{ fontSize: 11.5, color: M.inkDim }}>
            {status.has_api_key ? (status.api_key_masked || 'set') : 'not set'}
          </span>
        </div>
      </div>
      <div style={{ paddingTop: 8, display: 'flex', gap: 6 }}>
        <input className="mn-input" placeholder="paste Civitai API key…" type="password"
               value={apiKey} onChange={e => setApiKey(e.target.value)}
               onKeyDown={e => e.key === 'Enter' && saveKey()}
               style={{ flex: 1, fontSize: 12 }} />
        <button className="mn-btn mn-btn-accent"
                onClick={saveKey} disabled={!apiKey.trim()}
                style={{ padding: '6px 12px', fontSize: 12, opacity: apiKey.trim() ? 1 : 0.5 }}>
          {saved ? '✓' : 'Save'}
        </button>
      </div>
    </div>
  );
}
function SetCompactRow({ label, value, ok, last }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', padding: '7px 0', borderBottom: last ? 'none' : `1px solid ${M.lineSoft}` }}>
      <div style={{ fontSize: 12.5 }}>{label}</div>
      <div style={{ marginLeft: 'auto', display: 'flex', gap: 7, alignItems: 'center' }}>
        {ok !== undefined && <span style={{ width: 6, height: 6, borderRadius: '50%', background: ok ? M.ok : M.red }} />}
        <span className="mn-mono" style={{ fontSize: 11.5, color: M.inkDim }}>{value}</span>
      </div>
    </div>
  );
}

window.MonoSingleApp = MonoSingleApp;
