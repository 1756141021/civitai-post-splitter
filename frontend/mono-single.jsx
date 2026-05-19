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

const SORT_OPTS = [
  { value: 'random',    label: '随机' },
  { value: 'name_asc',  label: '文件名 A→Z' },
  { value: 'name_desc', label: '文件名 Z→A' },
  { value: 'time_desc', label: '最新优先' },
  { value: 'time_asc',  label: '最旧优先' },
  { value: 'manual',    label: '手动排序' },
];

const TARGETS_LS_KEY = 'civitai-splitter:upload-targets';
const ALL_TARGETS = ['civitai', 'pixiv', 'x', 'xhs'];

function _loadPersistedTargets(fallback) {
  try {
    const raw = localStorage.getItem(TARGETS_LS_KEY);
    if (!raw) return fallback;
    const arr = JSON.parse(raw);
    if (!Array.isArray(arr)) return fallback;
    const valid = arr.filter(t => ALL_TARGETS.includes(t));
    return valid.length > 0 ? valid : fallback;
  } catch {
    return fallback;
  }
}

function _savePersistedTargets(targetsList) {
  try {
    localStorage.setItem(TARGETS_LS_KEY, JSON.stringify(targetsList));
  } catch {}
}

function ImagePickerDialog({ cmd, llmConfig, uploadDefaults, onConfirm, onCancel, onReloadDefaults }) {
  const ud = uploadDefaults || {};
  const [images,       setImages]       = React.useState([]);
  const [selected,     setSelected]     = React.useState(new Set());
  const [loading,      setLoading]      = React.useState(true);
  const [uploading,    setUploading]    = React.useState(false);
  const [sortMode,     setSortMode]     = React.useState(ud.sort_mode || 'random');
  const [orderedFiles, setOrderedFiles] = React.useState([]);
  const [llmReverse,     setLlmReverse]     = React.useState(!!ud.llm_reverse);
  const [llmPersona,     setLlmPersona]     = React.useState(ud.llm_persona || '');
  const [llmContentMode, setLlmContentMode] = React.useState(ud.llm_content_mode || 'sfw');
  const [llmMode,               setLlmMode]               = React.useState(ud.llm_mode || 'unified');
  const [llmPersonasByPlatform, setLlmPersonasByPlatform] = React.useState(ud.llm_personas_by_platform || { pixiv: '', x: '', xhs: '' });
  const [llmContentByPlatform,  setLlmContentByPlatform]  = React.useState(ud.llm_content_modes_by_platform || { pixiv: ud.llm_content_mode || 'sfw', x: ud.llm_content_mode || 'sfw', xhs: ud.llm_content_mode || 'sfw' });
  const [xTemplate,    setXTemplate]    = React.useState(() => ud.x_template   ?? (localStorage.getItem('civitai-splitter:x-template')   || ''));
  const [xhsTemplate,  setXhsTemplate]  = React.useState(() => ud.xhs_template ?? (localStorage.getItem('civitai-splitter:xhs-template') || ''));
  const [aiTagsByPlatform, setAiTagsByPlatform] = React.useState(ud.ai_tags_by_platform || { pixiv: true, x: true, xhs: true });
  const [saving,    setSaving]    = React.useState(false);
  const [savedAt,   setSavedAt]   = React.useState(0);
  const [templateOpts, setTemplateOpts] = React.useState({ x: [], x_default: 'en_sfw', xhs: [], xhs_default: 'default' });
  const [pickN,        setPickN]        = React.useState('');
  const [uploadPage, setUploadPage] = React.useState(0);
  const [xhsPage,    setXhsPage]    = React.useState(0);
  const PAGE_SIZE = 24;
  const fileInputRef   = React.useRef(null);
  const dragItem       = React.useRef(null);
  const dragOverItem   = React.useRef(null);
  const prevSortMode   = React.useRef('random');

  // Default targets: uploadDefaults (backend) > legacy localStorage > cmd-based.
  const _cmdDefaultTargets = cmd === 3 ? ['xhs'] : ['civitai', 'pixiv'];
  const _initialTargets = Array.isArray(ud.targets) && ud.targets.length > 0
    ? ud.targets.filter(t => ALL_TARGETS.includes(t))
    : _loadPersistedTargets(_cmdDefaultTargets);
  const [targetCivitai, setTargetCivitai] = React.useState(_initialTargets.includes('civitai'));
  const [targetPixiv,   setTargetPixiv]   = React.useState(_initialTargets.includes('pixiv'));
  const [targetX,       setTargetX]       = React.useState(_initialTargets.includes('x'));
  const [targetXhs,     setTargetXhs]     = React.useState(_initialTargets.includes('xhs'));

  const label = 'Upload to selected platforms';
  const personas = (llmConfig && llmConfig.personas) || [];

  const applySortToImages = (imgs, mode) => {
    if (mode === 'name_asc')  return [...imgs].sort((a, b) => a.name.localeCompare(b.name));
    if (mode === 'name_desc') return [...imgs].sort((a, b) => b.name.localeCompare(a.name));
    if (mode === 'time_desc') return [...imgs].sort((a, b) => (b.mtime || 0) - (a.mtime || 0));
    if (mode === 'time_asc')  return [...imgs].sort((a, b) => (a.mtime || 0) - (b.mtime || 0));
    return imgs;
  };

  const sortedImages = React.useMemo(() => {
    if (sortMode === 'manual') return orderedFiles;
    return applySortToImages(images, sortMode);
  }, [images, sortMode, orderedFiles]);

  React.useEffect(() => {
    if (sortMode === 'manual' && prevSortMode.current !== 'manual') {
      setOrderedFiles(applySortToImages(images, prevSortMode.current));
    }
    prevSortMode.current = sortMode;
    setUploadPage(0);
    setXhsPage(0);
  }, [sortMode]);

  const loadImages = () =>
    fetch('/api/images').then(r => r.json()).then(list => {
      setImages(list);
      setSelected(prev => {
        if (prev.size === 0) {
          const autoSelect = cmd === 3 ? list.filter(f => f.source === 'xhs_upload') : list;
          return new Set(autoSelect.map(f => f.name));
        }
        return new Set([...prev].filter(n => list.some(f => f.name === n)));
      });
      setLoading(false);
    }).catch(() => setLoading(false));

  React.useEffect(() => { loadImages(); }, []);

  React.useEffect(() => {
    fetch('/api/templates').then(r => r.json()).then(data => {
      setTemplateOpts(data);
    }).catch(() => {});
  }, []);

  React.useEffect(() => {
    if (!llmConfig) return;
    const persona = (llmConfig.personas || []).find(p => p.id === llmConfig.default_persona_id) || (llmConfig.personas || [])[0] || {};
    const defaultId   = persona.id || '';
    const defaultMode = persona.default_content_mode || llmConfig.default_content_mode || 'sfw';
    if (!ud.llm_persona)      setLlmPersona(defaultId);
    if (!ud.llm_content_mode) setLlmContentMode(defaultMode);
    if (!ud.llm_personas_by_platform)      setLlmPersonasByPlatform({ pixiv: defaultId, x: defaultId, xhs: defaultId });
    if (!ud.llm_content_modes_by_platform) setLlmContentByPlatform({ pixiv: defaultMode, x: defaultMode, xhs: defaultMode });
  }, [llmConfig]);

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
    if (cmd === 3) fd.append('folder', 'xhs_upload');
    await fetch('/api/add-upload-files', { method: 'POST', body: fd });
    const newNames = files.map(f => f.name);
    await fetch('/api/images').then(r => r.json()).then(list => {
      setImages(list);
      setSelected(prev => { const next = new Set(prev); newNames.forEach(n => next.add(n)); return next; });
      if (sortMode === 'manual') setOrderedFiles(prev => {
        const nameSet = new Set(prev.map(f => f.name));
        const added = list.filter(f => newNames.includes(f.name) && !nameSet.has(f.name));
        return [...prev, ...added];
      });
    });
    setUploading(false);
    e.target.value = '';
  };

  const handleDragEnd = () => {
    const from = dragItem.current, to = dragOverItem.current;
    if (from === null || to === null || from === to) return;
    setOrderedFiles(prev => {
      const arr = [...prev];
      const [item] = arr.splice(from, 1);
      arr.splice(to, 0, item);
      return arr;
    });
    dragItem.current = null;
    dragOverItem.current = null;
  };

  const buildLlmOpts = () => {
    const templateFields = {
      ...(targetX   ? { x_template:   xTemplate   || templateOpts.x_default   || '' } : {}),
      ...(targetXhs ? { xhs_template: xhsTemplate || templateOpts.xhs_default || '' } : {}),
    };
    const aiFields = { ai_tags_by_platform: aiTagsByPlatform };
    if (llmMode === 'per_platform') {
      const platMap = { civitai: targetCivitai, pixiv: targetPixiv, x: targetX, xhs: targetXhs };
      const needsCopy = ['pixiv', 'x', 'xhs'].filter(p => platMap[p]);
      const pbp = {}, cbp = {};
      needsCopy.forEach(p => { pbp[p] = llmPersonasByPlatform[p] || ''; cbp[p] = llmContentByPlatform[p] || 'sfw'; });
      return { llm_reverse: llmReverse, llm_mode: 'per_platform', llm_personas_by_platform: pbp, llm_content_modes_by_platform: cbp, ...templateFields, ...aiFields };
    }
    return { llm_reverse: llmReverse, llm_mode: 'unified', llm_persona: llmPersona, llm_content_mode: llmContentMode, ...templateFields, ...aiFields };
  };

  const _currentTargets = () => [
    targetCivitai && 'civitai',
    targetPixiv && 'pixiv',
    targetX && 'x',
    targetXhs && 'xhs',
  ].filter(Boolean);

  const buildPayload = () => ({
    targets: _currentTargets(),
    sort_mode: sortMode,
    llm_reverse: llmReverse,
    llm_mode: llmMode,
    llm_persona: llmPersona,
    llm_content_mode: llmContentMode,
    llm_personas_by_platform: llmPersonasByPlatform,
    llm_content_modes_by_platform: llmContentByPlatform,
    x_template: xTemplate,
    xhs_template: xhsTemplate,
    ai_tags_by_platform: aiTagsByPlatform,
  });

  const _postDefaults = () =>
    fetch('/api/upload-defaults', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildPayload()),
    });

  const saveSettings = () => {
    setSaving(true);
    _postDefaults()
      .then(() => {
        setSaving(false);
        setSavedAt(Date.now());
        onReloadDefaults && onReloadDefaults();
        setTimeout(() => setSavedAt(0), 2000);
      })
      .catch(() => setSaving(false));
  };

  const go = async () => {
    const targetsList = _currentTargets();
    if (targetsList.length === 0) return;
    _savePersistedTargets(targetsList);
    try { await _postDefaults(); } catch {}
    onReloadDefaults && onReloadDefaults();
    const targets = targetsList.join(',');
    const llmOpts = buildLlmOpts();
    if (sortMode === 'manual') {
      onConfirm(cmd, orderedFiles.map(f => f.name), { sort: 'manual', targets, ...llmOpts });
      return;
    }
    const files = sortedImages.filter(f => selected.has(f.name)).map(f => f.name);
    onConfirm(cmd, files, { ...(sortMode !== 'random' ? { sort: sortMode } : {}), targets, ...llmOpts });
  };

  const selectTopN = n => {
    const cnt = Math.min(parseInt(n, 10) || 0, sortedImages.length);
    if (cnt > 0) setSelected(new Set(sortedImages.slice(0, cnt).map(f => f.name)));
  };

  const isManual = sortMode === 'manual';
  const imgUrl = f => `/${f.source || 'upload'}/${encodeURIComponent(f.name)}`;
  const uploadImgs = sortedImages.filter(f => f.source !== 'xhs_upload');
  const xhsImgs = sortedImages.filter(f => f.source === 'xhs_upload');
  const uploadSelCount = uploadImgs.filter(f => selected.has(f.name)).length;
  const xhsSelCount = xhsImgs.filter(f => selected.has(f.name)).length;
  const uploadEnabled = targetCivitai || targetPixiv || targetX;
  const xhsEnabled = targetXhs;
  const enabledSel = isManual ? orderedFiles.length : (uploadEnabled ? uploadSelCount : 0) + (xhsEnabled ? xhsSelCount : 0);
  const enabledAll = (uploadEnabled ? uploadImgs.length : 0) + (xhsEnabled ? xhsImgs.length : 0);
  const uploadCount = enabledSel;

  const _renderGrid = (files, page, setPage) => {
    const totalPages = Math.ceil(files.length / PAGE_SIZE) || 1;
    const safePage = Math.min(page, totalPages - 1);
    const paged = files.slice(safePage * PAGE_SIZE, (safePage + 1) * PAGE_SIZE);
    return (
      <>
        <div style={{ display:'grid', gridTemplateColumns:'repeat(auto-fill, minmax(120px,1fr))', gap:6 }}>
          {paged.map(f => {
            const sel = selected.has(f.name);
            return (
              <div key={`${f.source}:${f.name}`} onClick={() => toggle(f.name)}
                   style={{ cursor:'pointer', borderRadius:5, border:`2px solid ${sel ? M.accent : M.line}`,
                            overflow:'hidden', position:'relative', background:M.bg }}>
                <img src={imgUrl(f)} alt={f.name} loading="lazy"
                     style={{ width:'100%', aspectRatio:'1', objectFit:'cover', display:'block' }} />
                <div style={{ position:'absolute', top:3, right:3, width:14, height:14, borderRadius:'50%',
                              background: sel ? M.accent : 'rgba(0,0,0,0.45)', display:'grid', placeItems:'center' }}>
                  {sel && <span style={{ color:'#fff', fontSize:9, lineHeight:1 }}>✓</span>}
                </div>
                <div style={{ padding:'1px 3px', fontSize:8.5, fontFamily:M.mono, color:M.inkFaint,
                              whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis', background:M.panel }}>
                  {f.name}
                </div>
              </div>
            );
          })}
        </div>
        {totalPages > 1 && (
          <div style={{ display:'flex', justifyContent:'center', alignItems:'center', gap:10, padding:'8px 0', flexShrink:0 }}>
            <button className="mn-btn mn-btn-ghost" style={{ fontSize:12, padding:'2px 8px' }}
                    disabled={safePage === 0} onClick={() => setPage(safePage - 1)}>‹</button>
            <span className="mn-mono" style={{ fontSize:11, color:M.inkDim }}>{safePage + 1}/{totalPages}</span>
            <button className="mn-btn mn-btn-ghost" style={{ fontSize:12, padding:'2px 8px' }}
                    disabled={safePage >= totalPages - 1} onClick={() => setPage(safePage + 1)}>›</button>
          </div>
        )}
      </>
    );
  };

  return (
    <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 9999 }}>
      <div style={{ background: M.panel, borderRadius: 8, border: `1px solid ${M.line}`, width: 920, maxHeight: '90vh', display: 'flex', flexDirection: 'column' }}>
        {/* Header */}
        <div style={{ padding: '14px 18px 10px', borderBottom: `1px solid ${M.line}`, flexShrink: 0, display: 'flex', alignItems: 'baseline', gap: 12 }}>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 13.5, fontWeight: 600, marginBottom: 2 }}>{label}</div>
            <div className="mn-mono" style={{ fontSize: 11, color: M.inkDim }}>
              {loading ? '加载中…' : isManual ? `已排序 ${orderedFiles.length} 张` : `已选 ${enabledSel} 张`}
            </div>
          </div>
          <select className="mn-input" value={sortMode} onChange={e => setSortMode(e.target.value)} style={{ fontSize: 11, width: 100 }}>
            {SORT_OPTS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
          <input ref={fileInputRef} type="file" multiple accept="image/*" style={{ display: 'none' }} onChange={addFiles} />
          <button className="mn-btn mn-btn-ghost" style={{ fontSize: 11, padding: '3px 8px' }}
                  onClick={() => fileInputRef.current.click()} disabled={uploading}>
            {uploading ? '导入中…' : '+ 添加'}
          </button>
        </div>

        {/* ZONE 1: Dual-pane image grids (or manual mode) */}
        {!loading && isManual ? (
          <div style={{ flex: 1, overflow: 'auto', padding: 14 }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {orderedFiles.map((f, i) => (
                <div key={f.name} draggable
                     onDragStart={() => { dragItem.current = i; }}
                     onDragEnter={() => { dragOverItem.current = i; }}
                     onDragOver={e => e.preventDefault()}
                     onDragEnd={handleDragEnd}
                     style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '6px 8px', borderRadius: 6,
                              border: `1px solid ${M.line}`, background: M.bg, cursor: 'grab', userSelect: 'none' }}>
                  <span style={{ color: M.inkFaint, fontSize: 14, lineHeight: 1, cursor: 'grab' }}>⠿</span>
                  <span className="mn-mono" style={{ fontSize: 11, color: M.inkDim, minWidth: 24, textAlign: 'right' }}>{i + 1}</span>
                  <img src={imgUrl(f)} alt={f.name} loading="lazy"
                       style={{ width: 40, height: 40, objectFit: 'cover', borderRadius: 4, flexShrink: 0 }} />
                  <span style={{ fontSize: 12, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{f.name}</span>
                  <button className="mn-btn mn-btn-ghost" style={{ fontSize: 11, padding: '1px 6px', flexShrink: 0 }}
                          onClick={() => setOrderedFiles(prev => prev.filter((_, j) => j !== i))}>✕</button>
                </div>
              ))}
              {images.filter(f => !orderedFiles.some(o => o.name === f.name)).length > 0 && (
                <div style={{ marginTop: 8, borderTop: `1px dashed ${M.lineSoft}`, paddingTop: 10 }}>
                  <div style={{ fontSize: 11, color: M.inkDim, marginBottom: 8 }}>点击添加到队列末尾：</div>
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(80px, 1fr))', gap: 8 }}>
                    {images.filter(f => !orderedFiles.some(o => o.name === f.name)).map(f => (
                      <div key={f.name} onClick={() => setOrderedFiles(prev => [...prev, f])}
                           style={{ cursor: 'pointer', borderRadius: 6, border: `2px dashed ${M.line}`, overflow: 'hidden', background: M.bg, opacity: 0.7 }}>
                        <img src={imgUrl(f)} alt={f.name} loading="lazy"
                             style={{ width: '100%', aspectRatio: '1', objectFit: 'cover', display: 'block' }} />
                        <div style={{ padding: '2px 4px', fontSize: 10, fontFamily: M.mono, color: M.inkFaint,
                                      whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{f.name}</div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
        ) : (
          <div style={{ flex: 1, display: 'grid', gridTemplateColumns: cmd === 3 ? '1fr' : '1fr 1fr', minHeight: 0, overflow: 'hidden' }}>
            {/* Left pane: upload/ */}
            {cmd !== 3 && (
              <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden' }}>
                <div style={{ padding: '6px 10px', borderBottom: `1px solid ${M.lineSoft}`, display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
                  <span className="mn-mono" style={{ fontSize: 10.5, color: M.inkFaint }}>upload/</span>
                  <button className="mn-btn mn-btn-ghost" style={{ fontSize: 11, padding: '2px 8px' }}
                          onClick={() => setSelected(prev => { const n = new Set(prev); uploadImgs.forEach(f => n.add(f.name)); return n; })}>全选</button>
                  <button className="mn-btn mn-btn-ghost" style={{ fontSize: 11, padding: '2px 8px' }}
                          onClick={() => setSelected(prev => { const n = new Set(prev); uploadImgs.forEach(f => n.delete(f.name)); return n; })}>清空</button>
                  <span className="mn-mono" style={{ fontSize: 10.5, color: M.inkDim, marginLeft: 'auto' }}>{uploadSelCount}/{uploadImgs.length}</span>
                </div>
                <div style={{ flex: 1, overflowY: 'auto', overflowX: 'hidden', padding: 6 }}>
                  {loading ? <div style={{ textAlign: 'center', color: M.inkFaint, padding: 24, fontFamily: M.mono, fontSize: 12 }}>加载中…</div>
                    : uploadImgs.length === 0 ? <div style={{ textAlign: 'center', color: M.inkFaint, padding: 24, fontFamily: M.mono, fontSize: 11 }}>upload/ 为空</div>
                    : _renderGrid(uploadImgs, uploadPage, setUploadPage)}
                </div>
              </div>
            )}
            {/* Right pane: xhs_upload/ */}
            <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden', ...(cmd !== 3 ? { borderLeft: `1px solid ${M.line}` } : {}) }}>
              <div style={{ padding: '6px 10px', borderBottom: `1px solid ${M.lineSoft}`, display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
                <span className="mn-mono" style={{ fontSize: 10.5, color: M.inkFaint }}>xhs_upload/</span>
                <button className="mn-btn mn-btn-ghost" style={{ fontSize: 11, padding: '2px 8px' }}
                        onClick={() => setSelected(prev => { const n = new Set(prev); xhsImgs.forEach(f => n.add(f.name)); return n; })}>全选</button>
                <button className="mn-btn mn-btn-ghost" style={{ fontSize: 11, padding: '2px 8px' }}
                        onClick={() => setSelected(prev => { const n = new Set(prev); xhsImgs.forEach(f => n.delete(f.name)); return n; })}>清空</button>
                <span className="mn-mono" style={{ fontSize: 10.5, color: M.inkDim, marginLeft: 'auto' }}>{xhsSelCount}/{xhsImgs.length}</span>
              </div>
              <div style={{ flex: 1, overflowY: 'auto', overflowX: 'hidden', padding: 6 }}>
                {loading ? <div style={{ textAlign: 'center', color: M.inkFaint, padding: 24, fontFamily: M.mono, fontSize: 12 }}>加载中…</div>
                  : xhsImgs.length === 0 ? <div style={{ textAlign: 'center', color: M.inkFaint, padding: 24, fontFamily: M.mono, fontSize: 11 }}>xhs_upload/ 为空</div>
                  : _renderGrid(xhsImgs, xhsPage, setXhsPage)}
              </div>
            </div>
          </div>
        )}

        {/* ZONE 2: Unified progress bar */}
        {!isManual && (
          <div style={{ padding: '8px 14px', borderTop: `1px solid ${M.line}`, flexShrink: 0, display: 'flex', alignItems: 'center', gap: 10 }}>
            <div style={{ flex: 1, height: 5, borderRadius: 3, background: M.lineSoft, overflow: 'hidden' }}>
              <div style={{ width: enabledAll > 0 ? `${enabledSel / enabledAll * 100}%` : '0%', height: '100%', borderRadius: 3, background: M.accent, transition: 'width 0.2s ease' }} />
            </div>
            <span className="mn-mono" style={{ fontSize: 12, color: M.inkDim, whiteSpace: 'nowrap', minWidth: 44, textAlign: 'right' }}>{enabledSel}/{enabledAll}</span>
          </div>
        )}

        {/* ZONE 3: Controls */}
        <div style={{ padding: '8px 18px', borderTop: `1px solid ${M.lineSoft}`, display: 'flex', gap: 14, flexWrap: 'wrap', alignItems: 'center' }}>
          <span className="mn-mono" style={{ fontSize: 11, color: M.inkDim, marginRight: 4 }}>发布到</span>
          <label style={{ display: 'flex', gap: 5, alignItems: 'center', fontSize: 12, cursor: 'pointer' }}>
            <input type="checkbox" checked={targetCivitai} onChange={e => { const v = e.target.checked; setTargetCivitai(v); _savePersistedTargets([v&&'civitai', targetPixiv&&'pixiv', targetX&&'x', targetXhs&&'xhs'].filter(Boolean)); }} /> Civitai
          </label>
          <label style={{ display: 'flex', gap: 5, alignItems: 'center', fontSize: 12, cursor: 'pointer' }}>
            <input type="checkbox" checked={targetPixiv} onChange={e => { const v = e.target.checked; setTargetPixiv(v); _savePersistedTargets([targetCivitai&&'civitai', v&&'pixiv', targetX&&'x', targetXhs&&'xhs'].filter(Boolean)); }} /> Pixiv
          </label>
          <label style={{ display: 'flex', gap: 5, alignItems: 'center', fontSize: 12, cursor: 'pointer' }}>
            <input type="checkbox" checked={targetX} onChange={e => { const v = e.target.checked; setTargetX(v); _savePersistedTargets([targetCivitai&&'civitai', targetPixiv&&'pixiv', v&&'x', targetXhs&&'xhs'].filter(Boolean)); }} /> X
          </label>
          {targetX && templateOpts.x.length > 1 && (
            <select className="mn-input" value={xTemplate || templateOpts.x_default}
                    onChange={e => { setXTemplate(e.target.value); localStorage.setItem('civitai-splitter:x-template', e.target.value); }}
                    style={{ fontSize: 11, padding: '2px 4px' }}>
              {templateOpts.x.map(k => <option key={k} value={k}>{k}</option>)}
            </select>
          )}
          <label style={{ display: 'flex', gap: 5, alignItems: 'center', fontSize: 12, cursor: 'pointer' }}
                 title="NSFW 图自动跳过（小红书禁 R18）">
            <input type="checkbox" checked={targetXhs} onChange={e => { const v = e.target.checked; setTargetXhs(v); _savePersistedTargets([targetCivitai&&'civitai', targetPixiv&&'pixiv', targetX&&'x', v&&'xhs'].filter(Boolean)); }} /> 小红书
            <span className="mn-mono" style={{ fontSize: 10, color: M.inkDim, marginLeft: 2 }}>(NSFW 跳过)</span>
          </label>
        </div>

        {(targetPixiv || targetX || targetXhs) && (
          <div style={{ padding: '8px 18px', borderTop: `1px solid ${M.lineSoft}`, display: 'flex', gap: 14, flexWrap: 'wrap', alignItems: 'center' }}>
            <span className="mn-mono" style={{ fontSize: 11, color: M.inkDim, marginRight: 4 }}>AI标签</span>
            {targetPixiv && (
              <label style={{ display: 'flex', gap: 5, alignItems: 'center', fontSize: 12, cursor: 'pointer' }}>
                <input type="checkbox" checked={aiTagsByPlatform.pixiv !== false}
                       onChange={e => setAiTagsByPlatform(prev => ({ ...prev, pixiv: e.target.checked }))} /> Pixiv
              </label>
            )}
            {targetX && (
              <label style={{ display: 'flex', gap: 5, alignItems: 'center', fontSize: 12, cursor: 'pointer' }}>
                <input type="checkbox" checked={aiTagsByPlatform.x !== false}
                       onChange={e => setAiTagsByPlatform(prev => ({ ...prev, x: e.target.checked }))} /> X
              </label>
            )}
            {targetXhs && (
              <label style={{ display: 'flex', gap: 5, alignItems: 'center', fontSize: 12, cursor: 'pointer' }}>
                <input type="checkbox" checked={aiTagsByPlatform.xhs !== false}
                       onChange={e => setAiTagsByPlatform(prev => ({ ...prev, xhs: e.target.checked }))} /> 小红书
              </label>
            )}
          </div>
        )}

        {llmConfig && llmConfig.enabled && (
          <div style={{ padding: '8px 18px', borderTop: `1px solid ${M.lineSoft}` }}>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <label style={{ display: 'flex', gap: 6, alignItems: 'center', fontSize: 12, whiteSpace: 'nowrap' }}>
                <input type="checkbox" checked={llmReverse} onChange={e => setLlmReverse(e.target.checked)} />
                LLM 标题/简介
              </label>
              {llmReverse && (
                <button className="mn-btn mn-btn-ghost"
                        style={{ fontSize: 11, padding: '2px 8px', flexShrink: 0 }}
                        title={llmMode === 'unified' ? '切换为各平台分别设置' : '切换为统一人设'}
                        onClick={() => setLlmMode(m => m === 'unified' ? 'per_platform' : 'unified')}>
                  {llmMode === 'unified' ? '统一' : '各自'}
                </button>
              )}
              {llmReverse && llmMode === 'unified' && <>
                <select className="mn-input" value={llmPersona}
                        onChange={e => setLlmPersona(e.target.value)} style={{ fontSize: 12, flex: 1 }}>
                  {personas.map(p => <option key={p.id} value={p.id}>{p.label || p.id}</option>)}
                </select>
                <select className="mn-input" value={llmContentMode}
                        onChange={e => setLlmContentMode(e.target.value)} style={{ fontSize: 12, width: 76 }}>
                  <option value="sfw">SFW</option>
                  <option value="nsfw">NSFW</option>
                </select>
              </>}
            </div>
            {llmReverse && llmMode === 'per_platform' && (
              <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 4, paddingLeft: 4 }}>
                {[
                  targetPixiv && { key: 'pixiv', name: 'Pixiv' },
                  targetX     && { key: 'x',     name: 'X' },
                  targetXhs   && { key: 'xhs',   name: '小红书' },
                ].filter(Boolean).map(({ key, name }) => (
                  <div key={key} style={{ display: 'grid', gridTemplateColumns: '56px 1fr 76px', gap: 6, alignItems: 'center' }}>
                    <span style={{ fontSize: 11, color: M.inkDim }}>{name}</span>
                    <select className="mn-input" value={llmPersonasByPlatform[key] || ''}
                            onChange={e => setLlmPersonasByPlatform(prev => ({ ...prev, [key]: e.target.value }))}
                            style={{ fontSize: 12 }}>
                      {personas.map(p => <option key={p.id} value={p.id}>{p.label || p.id}</option>)}
                    </select>
                    <select className="mn-input" value={llmContentByPlatform[key] || 'sfw'}
                            onChange={e => setLlmContentByPlatform(prev => ({ ...prev, [key]: e.target.value }))}
                            style={{ fontSize: 12 }}>
                      <option value="sfw">SFW</option>
                      <option value="nsfw">NSFW</option>
                    </select>
                  </div>
                ))}
                {!targetPixiv && !targetX && !targetXhs && (
                  <span style={{ fontSize: 11, color: M.inkDim }}>没有需要文案的平台</span>
                )}
              </div>
            )}
          </div>
        )}

        <div style={{ padding: '10px 18px 14px', borderTop: `1px solid ${M.line}`, display: 'flex', gap: 8, alignItems: 'center' }}>
          {!isManual && (
            <button className="mn-btn mn-btn-ghost" style={{ fontSize: 12, marginRight: 'auto' }}
                    onClick={async () => {
                      const targetsList = _currentTargets();
                      if (targetsList.length === 0) return;
                      _savePersistedTargets(targetsList);
                      try { await _postDefaults(); } catch {}
                      onReloadDefaults && onReloadDefaults();
                      onConfirm(cmd, [], { sort: sortMode, targets: targetsList.join(','), ...buildLlmOpts() });
                    }} title="随机从 upload/ 选 1-5 张，排序方式遵循当前选项">
              随机 1-5
            </button>
          )}
          {isManual && <div style={{ marginRight: 'auto' }} />}
          <button className="mn-btn" onClick={onCancel}>取消</button>
          <button className="mn-btn mn-btn-ghost" onClick={saveSettings} disabled={saving}
                  style={{ minWidth: 64 }}>
            {saving ? '保存中…' : (savedAt > 0 ? '已保存 ✓' : '保存')}
          </button>
          <button className="mn-btn mn-btn-accent" onClick={go}
                  disabled={uploadCount === 0} style={{ opacity: uploadCount === 0 ? 0.5 : 1 }}>
            上传 {uploadCount} 张
          </button>
        </div>
      </div>
    </div>
  );
}

function TaggerSetupDialog({ onClose }) {
  const [haintag,         setHaintag]         = React.useState('');
  const [modelDir,        setModelDir]         = React.useState('');
  const [pixaiDir,        setPixaiDir]         = React.useState('');
  const [haintagOk,       setHaintagOk]        = React.useState(null);
  const [modelOk,         setModelOk]          = React.useState(null);
  const [pixaiOk,         setPixaiOk]          = React.useState(null);
  const [saving,          setSaving]           = React.useState(false);
  const [saved,           setSaved]            = React.useState(false);
  const [pixaiInstall,    setPixaiInstall]      = React.useState('idle'); // idle|running|done|error
  const [pixaiInstallErr, setPixaiInstallErr]  = React.useState('');
  const [clInstall,       setClInstall]        = React.useState('idle');
  const [clInstallErr,    setClInstallErr]     = React.useState('');
  const pixaiPollRef = React.useRef(null);
  const clPollRef    = React.useRef(null);

  React.useEffect(() => {
    fetch('/api/tagger-config').then(r => r.json()).then(d => {
      setHaintag(d.haintag_root || '');
      setModelDir(d.model_dir || '');
      setPixaiDir(d.pixai_model_dir || '');
      setHaintagOk(d.haintag_ok);
      setModelOk(d.model_ok);
      setPixaiOk(d.pixai_ok);
    }).catch(() => {});
    return () => {
      if (pixaiPollRef.current) clearInterval(pixaiPollRef.current);
      if (clPollRef.current)    clearInterval(clPollRef.current);
    };
  }, []);

  const startInstallWithPoll = (endpoint, statusEndpoint, targetDir, setDir, setOk, setState, setErr, pollRef) => {
    setState('running'); setErr('');
    fetch(endpoint, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(targetDir ? { target_dir: targetDir } : {}),
    })
      .then(r => r.json())
      .then(d => {
        if (!d.ok) { setState('error'); setErr(d.error || '启动失败'); return; }
        const taskId = d.task_id;
        if (d.target_dir && !targetDir) setDir(d.target_dir);
        pollRef.current = setInterval(() => {
          fetch(`${statusEndpoint}/${taskId}`)
            .then(r => r.json())
            .then(s => {
              if (s.status === 'done') {
                clearInterval(pollRef.current);
                setState('done');
                if (s.model_dir) setDir(s.model_dir);
                setOk(true);
              } else if (s.status === 'error') {
                clearInterval(pollRef.current);
                setState('error');
                setErr(s.error || '下载失败');
              }
            }).catch(() => {});
        }, 2000);
      })
      .catch(() => { setState('error'); setErr('请求失败'); });
  };

  const startPixaiInstall = () => startInstallWithPoll(
    '/api/install-pixai-tagger', '/api/install-pixai-tagger-status',
    pixaiDir, setPixaiDir, setPixaiOk, setPixaiInstall, setPixaiInstallErr, pixaiPollRef
  );
  const startClInstall = () => startInstallWithPoll(
    '/api/install-cl-tagger', '/api/install-cl-tagger-status',
    modelDir, setModelDir, setModelOk, setClInstall, setClInstallErr, clPollRef
  );

  // POST current inputs → server saves + checks paths → update ok indicators
  const postAndVerify = (closeAfter) => {
    setSaving(true);
    fetch('/api/tagger-config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ haintag_root: haintag, model_dir: modelDir, pixai_model_dir: pixaiDir }),
    })
      .then(r => r.json())
      .then(() => fetch('/api/tagger-config'))
      .then(r => r.json())
      .then(d => {
        setSaving(false);
        setHaintagOk(d.haintag_ok);
        setModelOk(d.model_ok);
        setPixaiOk(d.pixai_ok);
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

  const installBtnLabel = (state) =>
    state === 'running' ? '安装中…' : state === 'done' ? '✓ 已安装' : '一键安装';

  return (
    <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 9999 }}>
      <div style={{ background: M.panel, borderRadius: 8, border: `1px solid ${M.line}`, width: 540, padding: '20px 24px' }}>
        <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>打标器 配置</div>
        <div className="mn-mono" style={{ fontSize: 11, color: M.inkDim, marginBottom: 18 }}>
          PixAI 优先；均可选填，不填也能正常上传。输入框留空则安装到默认路径。
        </div>

        {/* PixAI model directory */}
        <div style={{ marginBottom: 16 }}>
          <div style={{ display: 'flex', alignItems: 'center', marginBottom: 5 }}>
            <span style={{ fontSize: 12.5 }}>PixAI 模型目录</span>
            <span className="mn-mono" style={{ marginLeft: 6, fontSize: 10.5, color: M.inkFaint }}>推荐 · deepghs/pixai-tagger-v0.9-onnx</span>
            {pixaiOk !== null && (
              <span style={{ marginLeft: 'auto', fontSize: 11, color: pixaiOk ? M.ok : M.red }}>
                {pixaiOk ? '✓ 已就绪' : pixaiDir ? '✗ 未找到 model.onnx' : '—'}
              </span>
            )}
          </div>
          <div style={{ display: 'flex', gap: 6 }}>
            <input className="mn-input" value={pixaiDir} onChange={e => setPixaiDir(e.target.value)}
                   placeholder="安装目录（留空用默认 models/pixai_tagger）"
                   style={{ flex: 1, fontSize: 12 }} disabled={pixaiInstall === 'running'} />
            <button className="mn-btn mn-btn-accent" onClick={startPixaiInstall}
                    disabled={pixaiInstall === 'running' || pixaiInstall === 'done'}
                    style={{ fontSize: 12, whiteSpace: 'nowrap', flexShrink: 0 }}>
              {installBtnLabel(pixaiInstall)}
            </button>
          </div>
          {pixaiInstall === 'running' && (
            <div className="mn-mono" style={{ fontSize: 10.5, color: M.inkDim, marginTop: 4 }}>
              正在从 HuggingFace 下载（约 1.27 GB），请稍候…
            </div>
          )}
          {pixaiInstall === 'error' && (
            <div className="mn-mono" style={{ fontSize: 10.5, color: M.red, marginTop: 4 }}>{pixaiInstallErr}</div>
          )}
          {pixaiInstall === 'idle' && (
            <div className="mn-mono" style={{ fontSize: 10.5, color: M.inkFaint, marginTop: 4 }}>
              需要 <span style={{ color: M.ink2 }}>pip install huggingface_hub</span>；或手动指定已有目录
            </div>
          )}
        </div>

        {/* WD14/CL model directory */}
        <div style={{ marginBottom: 16 }}>
          <div style={{ display: 'flex', alignItems: 'center', marginBottom: 5 }}>
            <span style={{ fontSize: 12.5 }}>WD14 模型目录</span>
            <span className="mn-mono" style={{ marginLeft: 6, fontSize: 10.5, color: M.inkFaint }}>fallback · SmilingWolf/wd-vit-tagger-v3</span>
            {modelOk !== null && (
              <span style={{ marginLeft: 'auto', fontSize: 11, color: modelOk ? M.ok : M.red }}>
                {modelOk ? '✓ 已就绪' : modelDir ? '✗ 缺少文件' : '—'}
              </span>
            )}
          </div>
          <div style={{ display: 'flex', gap: 6 }}>
            <input className="mn-input" value={modelDir} onChange={e => setModelDir(e.target.value)}
                   placeholder="安装目录（留空用默认 models/cl_tagger）"
                   style={{ flex: 1, fontSize: 12 }} disabled={clInstall === 'running'} />
            <button className="mn-btn" onClick={startClInstall}
                    disabled={clInstall === 'running' || clInstall === 'done'}
                    style={{ fontSize: 12, whiteSpace: 'nowrap', flexShrink: 0 }}>
              {installBtnLabel(clInstall)}
            </button>
          </div>
          {clInstall === 'running' && (
            <div className="mn-mono" style={{ fontSize: 10.5, color: M.inkDim, marginTop: 4 }}>
              正在下载 WD ViT Tagger v3，请稍候…
            </div>
          )}
          {clInstall === 'error' && (
            <div className="mn-mono" style={{ fontSize: 10.5, color: M.red, marginTop: 4 }}>{clInstallErr}</div>
          )}
          {clInstall === 'idle' && (
            <div className="mn-mono" style={{ fontSize: 10.5, color: M.inkFaint, marginTop: 4 }}>
              需要 <span style={{ color: M.ink2 }}>pip install huggingface_hub</span>；或指向 ComfyUI <span style={{ color: M.ink2 }}>models/onnx/cl_tagger</span>
            </div>
          )}
        </div>

        {/* haintag root */}
        <div style={{ marginBottom: 20 }}>
          <div style={{ display: 'flex', alignItems: 'center', marginBottom: 5 }}>
            <span style={{ fontSize: 12.5 }}>haintag 根目录</span>
            <span className="mn-mono" style={{ marginLeft: 6, fontSize: 10.5, color: M.inkFaint }}>可选</span>
            {haintagOk !== null && (
              <span style={{ marginLeft: 'auto', fontSize: 11, color: haintagOk ? M.ok : M.red }}>
                {haintagOk ? '✓ HainTag 已找到' : '✗ HainTag 未找到'}
              </span>
            )}
          </div>
          <input className="mn-input" value={haintag} onChange={e => setHaintag(e.target.value)}
                 placeholder="如 E:\projects\haintag\dist\HainTag" style={{ width: '100%', fontSize: 12 }} />
          <div className="mn-mono" style={{ fontSize: 10.5, color: M.inkFaint, marginTop: 4, lineHeight: 1.6 }}>
            填 HainTag 发布版目录（含 <span style={{ color: M.ink2 }}>HainTag.exe</span>）或源码根目录。
          </div>
        </div>

        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button className="mn-btn mn-btn-ghost" onClick={dismiss} style={{ fontSize: 12 }}>跳过</button>
          <button className="mn-btn" onClick={() => postAndVerify(false)} disabled={saving} style={{ fontSize: 12 }}>
            {saving ? '…' : '验证'}
          </button>
          <button className="mn-btn mn-btn-accent" onClick={() => postAndVerify(true)} disabled={saving} style={{ fontSize: 12 }}>
            {saved ? '✓ 已保存' : saving ? '…' : '保存'}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── LLM 反推：人设可视化编辑器 ────────────────────────────────
// 后端用 PLATFORM_SPECS 决定每个平台的输出字段（pixiv/x/xhs）。前端 fetch 一次
// 把 specs 拿过来，按 spec 动态渲染范文卡片的字段表单。前端不再有 JSON 文本框。

function genPersonaId() {
  const stamp = Date.now().toString(36);
  const rand = Math.random().toString(36).slice(2, 8);
  return `persona_${stamp}_${rand}`;
}

function emptySampleFields(spec) {
  const out = {};
  (spec?.fields || []).forEach(f => {
    out[f.key] = f.kind === 'tags' ? [] : '';
  });
  return out;
}

function newPersona(platformId, spec) {
  return {
    id: genPersonaId(),
    label: '新人设',
    platform: platformId,
    default_content_mode: 'sfw',
    voice: '',
    sfw_prompt: '',
    nsfw_prompt: '',
    extra_prompt: '',
    avoid: [],
    samples: [],
  };
}

function FieldHelp({ text }) {
  return <span title={text} style={{ marginLeft: 6, fontSize: 10, color: M.inkFaint, cursor: 'help' }}>?</span>;
}

function TagChips({ value, onChange, placeholder }) {
  const [draft, setDraft] = React.useState('');
  const items = Array.isArray(value) ? value : [];
  const add = () => {
    const v = draft.trim();
    if (!v) return;
    if (items.includes(v)) { setDraft(''); return; }
    onChange([...items, v]);
    setDraft('');
  };
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, alignItems: 'center', padding: 4, border: `1px solid ${M.line}`, borderRadius: 6, background: M.panel, minHeight: 32 }}>
      {items.map((t, i) => (
        <span key={i} className="mn-chip">
          {t}
          <span onClick={() => onChange(items.filter((_, j) => j !== i))} style={{ cursor: 'pointer', color: M.inkFaint }}>✕</span>
        </span>
      ))}
      <input value={draft} onChange={e => setDraft(e.target.value)}
             onKeyDown={e => { if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); add(); } }}
             onBlur={add}
             placeholder={placeholder || '回车添加'}
             style={{ flex: 1, minWidth: 80, border: 'none', outline: 'none', background: 'transparent',
                      fontFamily: M.mono, fontSize: 11, color: M.ink, padding: '2px 4px' }} />
    </div>
  );
}

function SampleFieldEditor({ field, value, onChange }) {
  const label = field.label || field.key;
  const counter = (field.kind === 'tags')
    ? `${(value || []).length}/${field.max_count || 10}`
    : `${(value || '').length}/${field.max || 0}`;
  return (
    <div style={{ marginBottom: 6 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 2 }}>
        <span className="mn-mono" style={{ fontSize: 10.5, color: M.inkDim }}>{label}</span>
        <span className="mn-mono" style={{ fontSize: 10, color: M.inkFaint }}>{counter}</span>
      </div>
      {field.kind === 'tags'
        ? <TagChips value={value || []} onChange={onChange} placeholder={`回车添加 ${label}`} />
        : field.kind === 'multiline'
          ? <textarea className="mn-input" value={value || ''} maxLength={field.max}
                       onChange={e => onChange(e.target.value)}
                       style={{ width: '100%', fontSize: 12, minHeight: 50, fontFamily: M.mono }} />
          : <input className="mn-input" value={value || ''} maxLength={field.max}
                    onChange={e => onChange(e.target.value)}
                    style={{ width: '100%', fontSize: 12 }} />}
    </div>
  );
}

function SampleCard({ sample, idx, spec, onChange, onRemove }) {
  return (
    <div style={{ border: `1px solid ${M.line}`, borderRadius: 6, padding: 10, background: M.bg, marginBottom: 8 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
        <span className="mn-mono" style={{ fontSize: 11, color: M.inkDim }}>#{idx + 1}</span>
        <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 11 }}>
          <input type="radio" checked={sample.mode === 'sfw'}
                 onChange={() => onChange({ ...sample, mode: 'sfw' })} /> SFW
        </label>
        <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 11 }}>
          <input type="radio" checked={sample.mode === 'nsfw'}
                 onChange={() => onChange({ ...sample, mode: 'nsfw' })} /> NSFW
        </label>
        <input className="mn-input" value={sample.note || ''}
               onChange={e => onChange({ ...sample, note: e.target.value })}
               placeholder="备注（可空）"
               style={{ fontSize: 11, flex: 1, marginLeft: 6 }} />
        <button className="mn-btn mn-btn-ghost" onClick={onRemove} style={{ fontSize: 11, padding: '2px 6px' }}>✕</button>
      </div>
      {(spec?.fields || []).map(field => (
        <SampleFieldEditor key={field.key} field={field}
                           value={(sample.fields || {})[field.key]}
                           onChange={v => onChange({ ...sample, fields: { ...(sample.fields || {}), [field.key]: v } })} />
      ))}
    </div>
  );
}

function LlmReverseDialog({ initialCfg, initialSpecs, onClose }) {
  const [cfg, setCfg] = React.useState(initialCfg);
  const specs = initialSpecs;
  const [activeId, setActiveId] = React.useState((initialCfg?.personas || [])[0]?.id || '');
  const [apiKey, setApiKey] = React.useState('');
  const [apiKeyEditing, setApiKeyEditing] = React.useState(false);
  const [clearKey, setClearKey] = React.useState(false);
  const [modelOpen, setModelOpen] = React.useState(
    !initialCfg?.has_api_key || !initialCfg?.base_url || !initialCfg?.model
  );
  const [saving, setSaving] = React.useState(false);
  const [msg, setMsg] = React.useState('');
  const [modelList,    setModelList]    = React.useState([]);
  const [fetchingMods, setFetchingMods] = React.useState(false);
  const [modFetchErr,  setModFetchErr]  = React.useState('');
  const [modelCustom,  setModelCustom]  = React.useState(false);

  const fetchModels = () => {
    setFetchingMods(true);
    setModFetchErr('');
    const params = { provider: cfg.provider || '', base_url: cfg.base_url || '' };
    if (apiKey.trim()) params.api_key = apiKey.trim();
    // 没传 api_key 时让后端 fallback 到 saved
    const qs = new URLSearchParams(params);
    fetch(`/api/llm-reverse-models?${qs}`)
      .then(r => r.json())
      .then(d => {
        setFetchingMods(false);
        if (d.error) { setModFetchErr(d.error); return; }
        setModelList(d.models || []);
        setModelCustom(false);
      })
      .catch(() => { setFetchingMods(false); setModFetchErr('请求失败'); });
  };

  const personas = cfg.personas || [];
  const active = personas.find(p => p.id === activeId) || personas[0] || null;
  const activeSpec = React.useMemo(() => {
    if (!active || !specs) return null;
    const plats = Array.isArray(active.platform) ? active.platform : [active.platform].filter(Boolean);
    if (plats.length === 0) return null;
    if (plats.length === 1) return specs[plats[0]] || null;
    const seen = new Set(), fields = [], extra = [];
    for (const pid of plats) {
      const s = specs[pid]; if (!s) continue;
      for (const f of (s.fields || [])) { if (!seen.has(f.key)) { seen.add(f.key); fields.push(f); } }
      for (const f of (s.extra_fields || [])) { if (!seen.has(f.key)) { seen.add(f.key); extra.push(f); } }
    }
    return fields.length ? { fields, extra_fields: extra } : specs[plats[0]] || null;
  }, [active, specs]);
  const platformIds = Object.keys(specs);

  const updatePersona = patch => {
    setCfg({
      ...cfg,
      personas: personas.map(p => p.id === active.id ? { ...p, ...patch } : p),
    });
  };

  const replacePersonas = next => {
    let nextActive = activeId;
    if (!next.find(p => p.id === activeId)) nextActive = next[0]?.id || '';
    setCfg({
      ...cfg,
      personas: next,
      default_persona_id: next.find(p => p.id === cfg.default_persona_id) ? cfg.default_persona_id : (next[0]?.id || ''),
    });
    setActiveId(nextActive);
  };

  const addPersona = () => {
    const p = newPersona('pixiv', specs.pixiv);
    p.label = `新人设 ${personas.length + 1}`;
    replacePersonas([...personas, p]);
    setActiveId(p.id);
  };

  const dupPersona = () => {
    if (!active) return;
    const copy = { ...JSON.parse(JSON.stringify(active)), id: genPersonaId(), label: `${active.label} 副本` };
    replacePersonas([...personas, copy]);
    setActiveId(copy.id);
  };

  const delPersona = () => {
    if (!active || personas.length <= 1) return;
    replacePersonas(personas.filter(p => p.id !== active.id));
  };

  const addSample = () => {
    if (!active) return;
    const s = { mode: active.default_content_mode || 'sfw', note: '', fields: emptySampleFields(activeSpec) };
    updatePersona({ samples: [...(active.samples || []), s] });
  };

  const save = () => {
    setSaving(true);
    setMsg('');
    const payload = { ...cfg };
    if (clearKey) payload.clear_api_key = true;
    else if (apiKey.trim()) payload.api_key = apiKey.trim();
    delete payload.has_api_key;
    delete payload.api_key_masked;
    fetch('/api/llm-reverse-config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
      .then(r => r.json().then(d => ({ ok: r.ok, d })))
      .then(({ ok, d }) => {
        setSaving(false);
        if (!ok) { setMsg(d.error || '保存失败'); return; }
        setCfg(d);
        setApiKey('');
        setClearKey(false);
        setMsg('已保存');
        setTimeout(() => onClose(true), 700);
      })
      .catch(() => { setSaving(false); setMsg('请求失败'); });
  };

  return (
    <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 9999 }}>
      <div style={{ background: M.panel, borderRadius: 8, border: `1px solid ${M.line}`, width: 880, maxHeight: '90vh', display: 'flex', flexDirection: 'column' }}>
        {/* Header */}
        <div style={{ padding: '16px 20px 12px', borderBottom: `1px solid ${M.line}`, display: 'flex', alignItems: 'center', gap: 12 }}>
          <div>
            <div style={{ fontSize: 14, fontWeight: 600 }}>LLM 反推 · 人设</div>
            <div className="mn-mono" style={{ fontSize: 10.5, color: M.inkDim, marginTop: 2 }}>
              图片 → 标题/简介。所有字段都是普通文本，前端不再让你写 JSON。
            </div>
          </div>
          <label style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 6, fontSize: 12.5 }}>
            <input type="checkbox" checked={!!cfg.enabled} onChange={e => setCfg({ ...cfg, enabled: e.target.checked })} />
            启用
          </label>
        </div>

        {/* Model connection (collapsible) */}
        <div style={{ borderBottom: `1px solid ${M.lineSoft}` }}>
          {(() => {
            const missing = [];
            if (!cfg.has_api_key) missing.push('API key');
            if (!cfg.base_url) missing.push('base URL');
            if (!cfg.model) missing.push('model');
            const incomplete = missing.length > 0;
            return (
              <div onClick={() => setModelOpen(!modelOpen)}
                   style={{ padding: '8px 20px', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: M.inkDim, userSelect: 'none' }}>
                <span style={{ fontSize: 10 }}>{modelOpen ? '▾' : '▸'}</span>
                模型连接
                <span className="mn-mono" style={{ marginLeft: 'auto', fontSize: 10.5, color: incomplete ? M.red : M.inkFaint }}>
                  {incomplete
                    ? `缺：${missing.join(' · ')}`
                    : `${cfg.model} · key ${cfg.api_key_masked}`}
                </span>
                {incomplete && (
                  <button className="mn-btn mn-btn-accent"
                          onClick={e => { e.stopPropagation(); setModelOpen(true); }}
                          style={{ fontSize: 11, padding: '2px 10px' }}>
                    去填 →
                  </button>
                )}
              </div>
            );
          })()}
          {modelOpen && (
            <div style={{ padding: '0 20px 12px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
              <select className="mn-input" value={cfg.provider || 'openai_compatible'}
                      onChange={e => {
                        const p = e.target.value;
                        const patch = { provider: p };
                        if (p === 'anthropic' && !cfg.base_url) patch.base_url = 'https://api.anthropic.com';
                        setCfg({ ...cfg, ...patch });
                      }} style={{ fontSize: 12 }}>
                <option value="openai_compatible">OpenAI 兼容</option>
                <option value="google_gemini">Google Gemini</option>
                <option value="anthropic">Anthropic (Claude)</option>
              </select>
              <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                {modelList.length > 0 && !modelCustom ? (
                  <select className="mn-input" value={cfg.model || ''} onChange={e => {
                    if (e.target.value === '__custom__') { setModelCustom(true); } else { setCfg({ ...cfg, model: e.target.value }); }
                  }} style={{ fontSize: 12, flex: 1 }}>
                    {!cfg.model && <option value="">选择模型…</option>}
                    {modelList.map(m => <option key={m} value={m}>{m}</option>)}
                    <option value="__custom__">自定义…</option>
                  </select>
                ) : (
                  <input className="mn-input" value={cfg.model || ''} onChange={e => setCfg({ ...cfg, model: e.target.value })}
                         placeholder={cfg.provider === 'google_gemini' ? 'gemini-2.5-flash' : cfg.provider === 'anthropic' ? 'claude-sonnet-4-6' : '模型名'}
                         style={{ fontSize: 12, flex: 1 }} />
                )}
                <button className="mn-btn mn-btn-ghost" onClick={fetchModels} disabled={fetchingMods}
                        title="从 API 获取可用模型列表"
                        style={{ fontSize: 11, padding: '3px 8px', whiteSpace: 'nowrap', flexShrink: 0 }}>
                  {fetchingMods ? '…' : '获取'}
                </button>
                {modFetchErr && <span style={{ fontSize: 10.5, color: M.red, maxWidth: 120, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={modFetchErr}>{modFetchErr}</span>}
              </div>
              <input className="mn-input" value={cfg.base_url || ''} onChange={e => setCfg({ ...cfg, base_url: e.target.value })}
                     placeholder={cfg.provider === 'anthropic' ? 'https://api.anthropic.com (留空用默认)' : cfg.provider === 'google_gemini' ? 'http://your-gemini-proxy' : 'base URL, e.g. https://api.example.com/v1'} style={{ fontSize: 12 }} />
              {clearKey ? (
                <div className="mn-input" style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 11.5, color: M.red }}>
                  <span>API key 已标记清空（保存生效）</span>
                  <button className="mn-btn mn-btn-ghost" onClick={() => setClearKey(false)}
                          style={{ marginLeft: 'auto', fontSize: 10.5, padding: '1px 8px' }}>撤销</button>
                </div>
              ) : cfg.has_api_key && !apiKey && !apiKeyEditing ? (
                <div className="mn-input" style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 11.5 }}>
                  <span style={{ color: M.ok }}>● 已保存</span>
                  <span className="mn-mono" style={{ color: M.inkDim }}>{cfg.api_key_masked}</span>
                  <button className="mn-btn mn-btn-ghost" onClick={() => setApiKeyEditing(true)}
                          style={{ marginLeft: 'auto', fontSize: 10.5, padding: '1px 8px' }}>修改</button>
                  <button className="mn-btn mn-btn-ghost" onClick={() => setClearKey(true)}
                          title="把已保存的 API key 清空"
                          style={{ fontSize: 10.5, padding: '1px 8px' }}>清空</button>
                </div>
              ) : (
                <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                  <input className="mn-input" type="password" value={apiKey} onChange={e => setApiKey(e.target.value)}
                         placeholder={cfg.has_api_key ? '输入新 API key 替换已保存的' : 'API key'}
                         autoFocus={apiKeyEditing}
                         style={{ fontSize: 12, flex: 1 }} />
                  {apiKeyEditing && (
                    <button className="mn-btn mn-btn-ghost" onClick={() => { setApiKey(''); setApiKeyEditing(false); }}
                            title="不修改，保留已保存的"
                            style={{ fontSize: 10.5, padding: '4px 8px', flexShrink: 0 }}>取消</button>
                  )}
                </div>
              )}
              <input className="mn-input" value={cfg.timeout_seconds || 45}
                     onChange={e => setCfg({ ...cfg, timeout_seconds: Number(e.target.value) || 45 })}
                     placeholder="超时 (秒)" style={{ fontSize: 12 }} />
            </div>
          )}
        </div>

        {/* Body: master / detail */}
        <div style={{ flex: 1, overflow: 'hidden', display: 'grid', gridTemplateColumns: '180px 1fr' }}>
          {/* Persona list */}
          <div style={{ borderRight: `1px solid ${M.line}`, overflow: 'auto', padding: '10px 8px' }}>
            {personas.map(p => (
              <div key={p.id} onClick={() => setActiveId(p.id)}
                   style={{ padding: '6px 10px', borderRadius: 4, cursor: 'pointer', marginBottom: 2,
                            background: p.id === activeId ? M.accentSoft : 'transparent',
                            color: p.id === activeId ? M.accent : M.ink, fontSize: 12.5 }}>
                <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{p.label || p.id}</div>
                <div className="mn-mono" style={{ fontSize: 9.5, color: M.inkFaint }}>
                  {(Array.isArray(p.platform) ? p.platform : [p.platform]).map(pid => specs[pid]?.label || pid).join(' / ')}
                </div>
              </div>
            ))}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginTop: 8, paddingTop: 8, borderTop: `1px solid ${M.lineSoft}` }}>
              <button className="mn-btn mn-btn-ghost" onClick={addPersona} style={{ fontSize: 11 }}>+ 新建</button>
              <button className="mn-btn mn-btn-ghost" onClick={dupPersona} disabled={!active} style={{ fontSize: 11 }}>⧉ 复制</button>
              <button className="mn-btn mn-btn-ghost" onClick={delPersona} disabled={!active || personas.length <= 1} style={{ fontSize: 11 }}>🗑 删除</button>
            </div>
          </div>

          {/* Active persona form */}
          <div style={{ overflow: 'auto', padding: '14px 18px' }}>
            {!active ? (
              <div style={{ color: M.inkFaint, fontSize: 12, textAlign: 'center', padding: 40 }}>
                还没有人设。点左下「+ 新建」加一个。
              </div>
            ) : <>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginBottom: 12 }}>
                <div>
                  <div style={{ fontSize: 11, color: M.inkDim, marginBottom: 3 }}>名字</div>
                  <input className="mn-input" value={active.label || ''}
                         onChange={e => updatePersona({ label: e.target.value })}
                         style={{ width: '100%', fontSize: 12.5 }} />
                </div>
                <div>
                  <div style={{ fontSize: 11, color: M.inkDim, marginBottom: 3 }}>
                    默认模式
                    <FieldHelp text="生成时默认走哪个模式；上传时仍可临时切换" />
                  </div>
                  <div style={{ display: 'flex', gap: 12, paddingTop: 6 }}>
                    {['sfw', 'nsfw'].map(m => (
                      <label key={m} style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 12 }}>
                        <input type="radio" checked={active.default_content_mode === m}
                               onChange={() => updatePersona({ default_content_mode: m })} /> {m.toUpperCase()}
                      </label>
                    ))}
                  </div>
                </div>
              </div>

              <div style={{ marginBottom: 12 }}>
                <div style={{ fontSize: 11, color: M.inkDim, marginBottom: 3 }}>
                  平台
                  <FieldHelp text="决定模型输出哪些字段，可多选。勾选多个时 LLM 一次生成所有平台的文案" />
                </div>
                <div style={{ display: 'flex', gap: 12 }}>
                  {platformIds.map(pid => {
                    const current = Array.isArray(active.platform) ? active.platform : [active.platform].filter(Boolean);
                    const checked = current.includes(pid);
                    return (
                      <label key={pid} style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 12 }}>
                        <input type="checkbox" checked={checked}
                               onChange={e => {
                                 const next = e.target.checked
                                   ? [...new Set([...current, pid])]
                                   : current.filter(p => p !== pid);
                                 if (next.length > 0) updatePersona({ platform: next });
                               }} />
                        {specs[pid].label}
                      </label>
                    );
                  })}
                </div>
              </div>

              <div style={{ marginBottom: 10 }}>
                <div style={{ fontSize: 11, color: M.inkDim, marginBottom: 3 }}>
                  语气描述
                  <FieldHelp text="自由文本，告诉 AI 你想要的语气。例：『短诗体，少用感叹号，避免堆砌形容词』" />
                </div>
                <textarea className="mn-input" value={active.voice || ''}
                          onChange={e => updatePersona({ voice: e.target.value })}
                          placeholder="例：短诗体标题，简介轻描淡写。语气克制，避免感叹号堆叠。"
                          style={{ width: '100%', fontSize: 12, minHeight: 50, fontFamily: M.mono }} />
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginBottom: 10 }}>
                <div>
                  <div style={{ fontSize: 11, color: M.inkDim, marginBottom: 3 }}>SFW 提示词</div>
                  <textarea className="mn-input" value={active.sfw_prompt || ''}
                            onChange={e => updatePersona({ sfw_prompt: e.target.value })}
                            placeholder="生成 SFW 文案时注入的额外指令"
                            style={{ width: '100%', fontSize: 11.5, minHeight: 60, fontFamily: M.mono }} />
                </div>
                <div>
                  <div style={{ fontSize: 11, color: M.inkDim, marginBottom: 3 }}>NSFW 提示词</div>
                  <textarea className="mn-input" value={active.nsfw_prompt || ''}
                            onChange={e => updatePersona({ nsfw_prompt: e.target.value })}
                            placeholder="生成 NSFW 文案时注入的额外指令"
                            style={{ width: '100%', fontSize: 11.5, minHeight: 60, fontFamily: M.mono }} />
                </div>
              </div>

              <div style={{ marginBottom: 10 }}>
                <div style={{ fontSize: 11, color: M.inkDim, marginBottom: 3 }}>
                  额外指令
                  <FieldHelp text="不分模式都会注入。常用于硬性约束（不要谈政治、不要识别真人）" />
                </div>
                <textarea className="mn-input" value={active.extra_prompt || ''}
                          onChange={e => updatePersona({ extra_prompt: e.target.value })}
                          style={{ width: '100%', fontSize: 11.5, minHeight: 50, fontFamily: M.mono }} />
              </div>

              <div style={{ marginBottom: 14 }}>
                <div style={{ fontSize: 11, color: M.inkDim, marginBottom: 3 }}>
                  屏蔽话题
                  <FieldHelp text="作为「不要谈这些」注入提示词。回车或逗号添加" />
                </div>
                <TagChips value={active.avoid || []} onChange={v => updatePersona({ avoid: v })} placeholder="回车添加屏蔽词" />
              </div>

              <div style={{ marginBottom: 12, paddingTop: 12, borderTop: `1px solid ${M.lineSoft}` }}>
                <div style={{ display: 'flex', alignItems: 'center', marginBottom: 8 }}>
                  <div style={{ fontSize: 12, fontWeight: 600 }}>
                    范文 ({(active.samples || []).length})
                    <FieldHelp text="贴几条理想输出当样例，AI 会按对应模式拿来 few-shot 模仿。SFW 范文只在 SFW 时用，反之亦然。" />
                  </div>
                  <button className="mn-btn mn-btn-ghost" onClick={addSample} style={{ fontSize: 11, marginLeft: 'auto' }}>+ 加一例</button>
                </div>
                {(active.samples || []).length === 0 && (
                  <div style={{ color: M.inkFaint, fontSize: 11.5, textAlign: 'center', padding: 16, border: `1px dashed ${M.line}`, borderRadius: 6 }}>
                    还没有范文。贴几条理想标题/简介让 AI 模仿。
                  </div>
                )}
                {(active.samples || []).map((s, i) => (
                  <SampleCard key={i} sample={s} idx={i} spec={activeSpec}
                              onChange={ns => {
                                const arr = [...(active.samples || [])];
                                arr[i] = ns;
                                updatePersona({ samples: arr });
                              }}
                              onRemove={() => {
                                const arr = (active.samples || []).filter((_, j) => j !== i);
                                updatePersona({ samples: arr });
                              }} />
                ))}
              </div>
            </>}
          </div>
        </div>

        {/* Footer */}
        <div style={{ padding: '10px 20px', borderTop: `1px solid ${M.line}`, display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 11, color: M.inkDim }}>默认人设：</span>
          <select className="mn-input" value={cfg.default_persona_id || ''}
                  onChange={e => setCfg({ ...cfg, default_persona_id: e.target.value })}
                  style={{ fontSize: 12, minWidth: 180 }}>
            {personas.map(p => <option key={p.id} value={p.id}>{p.label || p.id}</option>)}
          </select>
          {msg && <span className="mn-mono" style={{ fontSize: 11, color: msg.includes('失败') || msg.includes('错误') ? M.red : M.ok }}>{msg}</span>}
          <div style={{ marginLeft: 'auto', display: 'flex', gap: 8 }}>
            <button className="mn-btn" onClick={() => onClose(false)}>取消</button>
            <button className="mn-btn mn-btn-accent" onClick={save} disabled={saving}>{saving ? '…' : '保存'}</button>
          </div>
        </div>
      </div>
    </div>
  );
}

function SchedulerDialog({ current, llmConfig, onClose, onSave }) {
  const sched = current || {};
  const [minHours,      setMinHours]      = React.useState(String(sched.min_hours ?? 0.4));
  const [maxHours,      setMaxHours]      = React.useState(String(sched.max_hours ?? 0.8));
  const [count,         setCount]         = React.useState(String(sched.count ?? 1));
  const [sortMode,      setSortMode]      = React.useState(sched.sort || 'random');
  const _initialTargetsCsv = sched.targets || 'civitai,pixiv';
  const [civitai,       setCivitai]       = React.useState(_initialTargetsCsv.includes('civitai'));
  const [pixiv,         setPixiv]         = React.useState(_initialTargetsCsv.includes('pixiv'));
  // .split(',') prevents 'x' matching 'civitai' / 'xhs' substring false-positive.
  const _targetsArr = _initialTargetsCsv.split(',').map(s => s.trim());
  const [xTarget,       setXTarget]       = React.useState(_targetsArr.includes('x'));
  const [xhs,           setXhs]           = React.useState(_targetsArr.includes('xhs'));
  const [llmReverse,    setLlmReverse]    = React.useState(!!sched.llm_reverse);
  const [llmPersona,    setLlmPersona]    = React.useState(sched.llm_persona || '');
  const [llmContentMode,setLlmContentMode]= React.useState(sched.llm_content_mode || 'sfw');
  const [xhsLlmPersona,    setXhsLlmPersona]    = React.useState(sched.xhs_llm_persona || '');
  const [xhsLlmContentMode,setXhsLlmContentMode]= React.useState(sched.xhs_llm_content_mode || '');
  const _initAiTags = sched.ai_tags_by_platform || { pixiv: true, x: true, xhs: true };
  const [aiPixiv,       setAiPixiv]       = React.useState(_initAiTags.pixiv !== false);
  const [aiX,           setAiX]           = React.useState(_initAiTags.x !== false);
  const [aiXhs,         setAiXhs]         = React.useState(_initAiTags.xhs !== false);
  const [saving,        setSaving]        = React.useState(false);
  const [err,           setErr]           = React.useState('');

  const personas = (llmConfig && llmConfig.personas) || [];
  const llmEnabled = llmConfig && llmConfig.enabled;

  const submit = () => {
    const min = parseFloat(minHours), max = parseFloat(maxHours), cnt = parseInt(count, 10);
    if (!min || !max || min <= 0 || max <= 0 || min > max) { setErr('时间范围无效（min 需 ≤ max）'); return; }
    if (!cnt || cnt < 1) { setErr('张数至少 1'); return; }
    const targets = [civitai && 'civitai', pixiv && 'pixiv', xTarget && 'x', xhs && 'xhs'].filter(Boolean).join(',');
    if (!targets) { setErr('至少选一个目标'); return; }
    setSaving(true); setErr('');
    fetch('/api/scheduler', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        enabled: true, min_hours: min, max_hours: max, count: cnt, targets, sort: sortMode,
        llm_reverse: llmReverse, llm_persona: llmPersona, llm_content_mode: llmContentMode,
        xhs_llm_persona: xhsLlmPersona, xhs_llm_content_mode: xhsLlmContentMode,
        ai_tags_by_platform: { pixiv: aiPixiv, x: aiX, xhs: aiXhs },
      }),
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

        <div style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 12.5, marginBottom: 6 }}>选图排序</div>
          <select className="mn-input" value={sortMode} onChange={e => setSortMode(e.target.value)} style={{ fontSize: 12, width: '100%' }}>
            {SORT_OPTS.filter(o => o.value !== 'manual').map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </div>

        <div style={{ marginBottom: llmEnabled ? 12 : 18 }}>
          <div style={{ fontSize: 12.5, marginBottom: 6 }}>发布目标</div>
          <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap' }}>
            <label style={{ display: 'flex', gap: 6, alignItems: 'center', fontSize: 12.5, cursor: 'pointer' }}>
              <input type="checkbox" checked={civitai} onChange={e => setCivitai(e.target.checked)} /> Civitai
            </label>
            <label style={{ display: 'flex', gap: 6, alignItems: 'center', fontSize: 12.5, cursor: 'pointer' }}>
              <input type="checkbox" checked={pixiv} onChange={e => setPixiv(e.target.checked)} /> Pixiv
            </label>
            <label style={{ display: 'flex', gap: 6, alignItems: 'center', fontSize: 12.5, cursor: 'pointer' }}>
              <input type="checkbox" checked={xTarget} onChange={e => setXTarget(e.target.checked)} /> X
            </label>
            <label style={{ display: 'flex', gap: 6, alignItems: 'center', fontSize: 12.5, cursor: 'pointer' }}
                   title="NSFW 图自动跳过（小红书禁 R18）">
              <input type="checkbox" checked={xhs} onChange={e => setXhs(e.target.checked)} /> 小红书
            </label>
          </div>
        </div>

        {(pixiv || xTarget || xhs) && (
          <div style={{ marginBottom: 14 }}>
            <div style={{ fontSize: 12.5, marginBottom: 6 }}>AI标签</div>
            <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap' }}>
              {pixiv && <label style={{ display: 'flex', gap: 6, alignItems: 'center', fontSize: 12.5, cursor: 'pointer' }}>
                <input type="checkbox" checked={aiPixiv} onChange={e => setAiPixiv(e.target.checked)} /> Pixiv
              </label>}
              {xTarget && <label style={{ display: 'flex', gap: 6, alignItems: 'center', fontSize: 12.5, cursor: 'pointer' }}>
                <input type="checkbox" checked={aiX} onChange={e => setAiX(e.target.checked)} /> X
              </label>}
              {xhs && <label style={{ display: 'flex', gap: 6, alignItems: 'center', fontSize: 12.5, cursor: 'pointer' }}>
                <input type="checkbox" checked={aiXhs} onChange={e => setAiXhs(e.target.checked)} /> 小红书
              </label>}
            </div>
          </div>
        )}

        {llmEnabled && (
          <div style={{ marginBottom: 18 }}>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <label style={{ display: 'flex', gap: 6, alignItems: 'center', fontSize: 12.5, cursor: 'pointer', whiteSpace: 'nowrap' }}>
                <input type="checkbox" checked={llmReverse} onChange={e => setLlmReverse(e.target.checked)} />
                LLM 标题/简介
              </label>
              {llmReverse && <>
                <select className="mn-input" value={llmPersona} onChange={e => setLlmPersona(e.target.value)}
                        style={{ fontSize: 12, flex: 1 }}>
                  <option value="">（默认人设）</option>
                  {personas.map(p => <option key={p.id} value={p.id}>{p.label || p.id}</option>)}
                </select>
                <select className="mn-input" value={llmContentMode} onChange={e => setLlmContentMode(e.target.value)}
                        style={{ fontSize: 12, width: 76 }}>
                  <option value="sfw">SFW</option>
                  <option value="nsfw">NSFW</option>
                </select>
              </>}
            </div>
            {llmReverse && xhs && (
              <div style={{ marginTop: 6, display: 'grid', gridTemplateColumns: '56px 1fr 76px', gap: 6, alignItems: 'center', paddingLeft: 4 }}>
                <span style={{ fontSize: 11, color: M.inkDim }}>小红书</span>
                <select className="mn-input" value={xhsLlmPersona} onChange={e => setXhsLlmPersona(e.target.value)}
                        style={{ fontSize: 12 }}>
                  <option value="">（同上）</option>
                  {personas.map(p => <option key={p.id} value={p.id}>{p.label || p.id}</option>)}
                </select>
                <select className="mn-input" value={xhsLlmContentMode} onChange={e => setXhsLlmContentMode(e.target.value)}
                        style={{ fontSize: 12 }}>
                  <option value="">同上</option>
                  <option value="sfw">SFW</option>
                  <option value="nsfw">NSFW</option>
                </select>
              </div>
            )}
          </div>
        )}

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
  const [llmReverseDialog, setLlmReverseDialog] = React.useState(false);
  const [llmReverseConfig, setLlmReverseConfig] = React.useState(null);
  const [llmSpecs, setLlmSpecs] = React.useState(null);
  const [status, setStatus] = React.useState({ mosaic_installed: false, upload_count: 0, has_api_key: false, pixiv_logged_in: false, civitai_logged_in: false, llm_reverse_enabled: false, llm_reverse_configured: false, scheduler: { enabled: false, next_fire_at: null, min_hours: 0.4, max_hours: 0.8, count: 1, sort: 'random', targets: 'civitai,pixiv', llm_reverse: false, llm_persona: '', llm_account: '', llm_content_mode: '' } });
  const [isDark, setIsDark] = React.useState(() => localStorage.getItem('mn-theme') === 'dark');
  const [pageDragging, setPageDragging] = React.useState(false);
  const [dropToast,    setDropToast]    = React.useState('');

  React.useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), 800);
    return () => clearInterval(id);
  }, []);

  React.useEffect(() => {
    fetch('/api/status').then(r => r.json()).then(setStatus).catch(() => {});
    fetch('/api/llm-reverse-config').then(r => r.json()).then(setLlmReverseConfig).catch(() => {});
    fetch('/api/llm-reverse-platforms').then(r => r.json()).then(setLlmSpecs).catch(() => {});
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
    es.addEventListener('status_update', e => {
      const s = JSON.parse(e.data);
      setStatus(prev => ({ ...prev, ...s }));
      if (s.civitai_logged_in) setCivitaiOpening(false);
      if (s.pixiv_logged_in) setPixivOpening(false);
      if (s.xhs_logged_in) setXhsOpening(false);
    });
    let errTimer = null;
    es.onopen  = () => { clearTimeout(errTimer); setConnected(true); };
    es.onerror = () => { errTimer = setTimeout(() => { if (es.readyState !== 1) setConnected(false); }, 2000); };
    return () => {
      window.removeEventListener('pagehide', notifyShutdown);
      window.removeEventListener('beforeunload', notifyShutdown);
      es.close();
    };
  }, []);

  React.useEffect(() => {
    const hasFiles = e => e.dataTransfer && [...e.dataTransfer.types].includes('Files');
    const onEnter = e => { if (hasFiles(e)) { e.preventDefault(); setPageDragging(true); } };
    const onOver  = e => { if (hasFiles(e)) e.preventDefault(); };
    const onLeave = e => { if (!e.relatedTarget) setPageDragging(false); };
    const onDrop  = async e => {
      e.preventDefault();
      setPageDragging(false);
      const files = Array.from(e.dataTransfer.files);
      if (!files.length) return;
      const fd = new FormData();
      files.forEach(f => fd.append('files', f));
      try {
        const r = await fetch('/api/add-upload-files', { method: 'POST', body: fd });
        const data = await r.json();
        const n = (data.saved || []).length;
        if (n > 0) { setDropToast(`已添加 ${n} 张`); setTimeout(() => setDropToast(''), 2000); }
      } catch (_) {}
    };
    document.addEventListener('dragenter', onEnter);
    document.addEventListener('dragover',  onOver);
    document.addEventListener('dragleave', onLeave);
    document.addEventListener('drop',      onDrop);
    return () => {
      document.removeEventListener('dragenter', onEnter);
      document.removeEventListener('dragover',  onOver);
      document.removeEventListener('dragleave', onLeave);
      document.removeEventListener('drop',      onDrop);
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

  const cancelTask = id => {
    setPendingInput(prev => (prev && prev.task_id === id ? null : prev));
    fetch(`/api/tasks/${id}/cancel`, { method: 'POST' });
  };

  const removeTask = id => {
    fetch(`/api/tasks/${id}/remove`, { method: 'POST' });
    setTasks(prev => prev.filter(t => t.id !== id));
  };

  const retryTask = (id, cmd) => {
    const t = tasks.find(x => x.id === id);
    const params = (t && t.params) || {};
    removeTask(id);
    runCmd(cmd, params);
  };

  const reloadStatus = () =>
    fetch('/api/status').then(r => r.json()).then(setStatus).catch(() => {});

  const startUpload = cmd => setUploadDialog({ cmd });
  const confirmUpload = (cmd, files, options = {}) => {
    setUploadDialog(null);
    runCmd(cmd, { ...(files && files.length > 0 ? { files } : {}), ...options });
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
          llmConfig={llmReverseConfig}
          uploadDefaults={status.upload_defaults || {}}
          onConfirm={confirmUpload}
          onCancel={() => setUploadDialog(null)}
          onReloadDefaults={reloadStatus}
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
          llmConfig={llmReverseConfig}
          onClose={() => setSchedulerDialog(false)}
          onSave={() => fetch('/api/status').then(r => r.json()).then(setStatus).catch(() => {})}
        />
      )}
      {llmReverseDialog && (
        llmReverseConfig && llmSpecs
          ? <LlmReverseDialog initialCfg={llmReverseConfig} initialSpecs={llmSpecs} onClose={saved => {
              setLlmReverseDialog(false);
              if (saved) {
                fetch('/api/llm-reverse-config').then(r => r.json()).then(setLlmReverseConfig).catch(() => {});
                fetch('/api/status').then(r => r.json()).then(setStatus).catch(() => {});
              }
            }} />
          : <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 9999 }}
                 onClick={() => setLlmReverseDialog(false)}>
              <div style={{ background: M.panel, borderRadius: 8, border: `1px solid ${M.line}`, padding: '32px 48px', color: M.inkDim, fontSize: 13, fontFamily: M.mono }}>
                加载中…
              </div>
            </div>
      )}

      {pageDragging && (
        <div style={{ position: 'fixed', inset: 0, zIndex: 800, background: 'rgba(0,120,212,0.18)', border: '3px dashed #0078d4', display: 'flex', alignItems: 'center', justifyContent: 'center', pointerEvents: 'none' }}>
          <div style={{ fontSize: 24, color: '#0078d4', fontWeight: 600 }}>拖拽图片添加到上传队列</div>
        </div>
      )}
      {dropToast && (
        <div style={{ position: 'fixed', bottom: 40, left: '50%', transform: 'translateX(-50%)', zIndex: 900, background: '#0078d4', color: '#fff', padding: '8px 20px', borderRadius: 8, fontSize: 14, fontWeight: 500, pointerEvents: 'none' }}>
          {dropToast}
        </div>
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
                       onSchedulerConfigure={() => setSchedulerDialog(true)}
                       onLlmReverseConfigure={() => {
                         setLlmReverseDialog(true);
                         if (!llmReverseConfig) {
                           fetch('/api/llm-reverse-config').then(r => r.json()).then(setLlmReverseConfig).catch(() => {});
                         }
                         if (!llmSpecs) {
                           fetch('/api/llm-reverse-platforms').then(r => r.json()).then(setLlmSpecs).catch(() => {});
                         }
                       }} />
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
  const _t2 = _loadPersistedTargets(['civitai', 'pixiv']).join(' + ');
  const ops = [
    { key: '1', icon: 'split',   title: 'Split post',     sub: '一帖多图 → 多帖单图',    cmd: 1 },
    { key: '2', icon: 'upload',  title: '多站发布',        sub: _t2 || 'Civitai + Pixiv', cmd: 2, upload: true },
    { key: '3', icon: 'image',   title: '小红书发布',      sub: 'xhs_upload/',            cmd: 3, upload: true },
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
function SettingsZone({ status, onStatusReload, taggerConfigured, onTaggerSetup, tick, onSchedulerConfigure, onLlmReverseConfigure }) {
  const [apiKey,        setApiKey]        = React.useState('');
  const [saved,         setSaved]         = React.useState(false);
  const [pixivSwitching,   setPixivSwitching]   = React.useState(false);
  const [pixivMsg,         setPixivMsg]         = React.useState('');
  const [civitaiSwitching, setCivitaiSwitching] = React.useState(false);
  const [civitaiMsg,       setCivitaiMsg]       = React.useState('');
  const [pixivOpening,     setPixivOpening]     = React.useState(false);
  const [pixivGuideOpen,   setPixivGuideOpen]   = React.useState(false);
  const [civitaiOpening,   setCivitaiOpening]   = React.useState(false);
  const [civitaiGuideOpen, setCivitaiGuideOpen] = React.useState(false);
  const [xCookies,   setXCookies]   = React.useState('');
  const [xSaving,    setXSaving]    = React.useState(false);
  const [xMsg,       setXMsg]       = React.useState('');
  const [xGuideOpen,  setXGuideOpen]  = React.useState(false);
  const [xTabMethod, setXTabMethod] = React.useState('full');
  const [xAuthToken, setXAuthToken] = React.useState('');
  const [xCt0,       setXCt0]       = React.useState('');
  const [xhsMsg,     setXhsMsg]     = React.useState('');
  const [xhsOpening, setXhsOpening] = React.useState(false);
  const [xhsGuideOpen, setXhsGuideOpen] = React.useState(false);

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
      <div className="ms-section-label" style={{ marginBottom: 8 }}>设置</div>
      <SetCompactRow label="马赛克模型" value={status.mosaic_installed ? '已安装' : '未安装'} ok={status.mosaic_installed} />
      <div style={{ display: 'flex', alignItems: 'center', padding: '7px 0', borderBottom: `1px solid ${M.lineSoft}` }}>
        <div style={{ fontSize: 12.5 }}>打码档位</div>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
          <select className="mn-input" value={status.censor_preset || 'japan'}
                  disabled={!status.mosaic_installed}
                  onChange={e => {
                    const v = e.target.value;
                    fetch('/api/censor-preset', {
                      method: 'POST',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({ preset: v }),
                    }).then(() => onStatusReload && onStatusReload());
                  }}
                  style={{ fontSize: 11.5, padding: '2px 6px' }}>
            <option value="off">关</option>
            <option value="japan">Pixiv 标准</option>
            <option value="strict">严格</option>
          </select>
        </div>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', padding: '7px 0', borderBottom: `1px solid ${M.lineSoft}` }}>
        <div style={{ fontSize: 12.5 }}>WD14 tagger</div>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
          <span style={{ width: 6, height: 6, borderRadius: '50%', background: taggerConfigured ? M.ok : M.red }} />
          <span className="mn-mono" style={{ fontSize: 11.5, color: M.inkDim }}>{taggerConfigured ? '已配置' : '未设置'}</span>
          <button className="mn-btn mn-btn-ghost" onClick={onTaggerSetup} style={{ padding: '2px 8px', fontSize: 11 }}>配置</button>
        </div>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', padding: '7px 0', borderBottom: `1px solid ${M.lineSoft}` }}>
        <div style={{ fontSize: 12.5 }}>LLM reverse</div>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
          <span style={{ width: 6, height: 6, borderRadius: '50%', background: status.llm_reverse_enabled && status.llm_reverse_configured ? M.ok : M.inkFaint }} />
          <span className="mn-mono" style={{ fontSize: 11.5, color: M.inkDim }}>
            {status.llm_reverse_enabled ? (status.llm_reverse_configured ? (status.llm_reverse_model || '已配置') : '未设置') : '关闭'}
          </span>
          <button className="mn-btn mn-btn-ghost" onClick={onLlmReverseConfigure} style={{ padding: '2px 8px', fontSize: 11 }}>配置</button>
        </div>
      </div>
      <div style={{ padding: '7px 0', borderBottom: `1px solid ${M.lineSoft}` }}>
        <div style={{ display: 'flex', alignItems: 'center' }}>
          <div style={{ fontSize: 12.5 }}>Pixiv 账号</div>
          <div style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
            {pixivMsg && <span className="mn-mono" style={{ fontSize: 10.5, color: M.red }}>{pixivMsg}</span>}
            <span style={{ width: 6, height: 6, borderRadius: '50%', background: status.pixiv_logged_in ? M.ok : M.inkFaint }} />
            <span className="mn-mono" style={{ fontSize: 11.5, color: M.inkDim }}>{status.pixiv_logged_in ? '已登录' : '未设置'}</span>
            {status.pixiv_logged_in ? (
              <button className="mn-btn mn-btn-ghost" disabled={pixivSwitching}
                      onClick={() => {
                        setPixivSwitching(true); setPixivMsg('');
                        fetch('/api/pixiv-logout', { method: 'POST' })
                          .then(r => r.json().then(d => ({ ok: r.ok, d })))
                          .then(({ ok, d }) => { setPixivSwitching(false); if (ok) { onStatusReload && onStatusReload(); } else setPixivMsg(d.error === 'pixiv task is running' ? '停止当前任务后再切换' : d.error); })
                          .catch(() => { setPixivSwitching(false); setPixivMsg('请求失败'); });
                      }}
                      style={{ padding: '2px 8px', fontSize: 11 }}>
                {pixivSwitching ? '…' : '切换账号'}
              </button>
            ) : (
              <button className="mn-btn mn-btn-ghost" onClick={() => setPixivGuideOpen(v => !v)}
                      style={{ padding: '2px 8px', fontSize: 11 }}>
                {pixivGuideOpen ? '设置 ▴' : '设置 ▾'}
              </button>
            )}
          </div>
        </div>
        {!status.pixiv_logged_in && pixivGuideOpen && (
          <div style={{ marginTop: 8, fontSize: 11.5, color: M.inkDim, lineHeight: 1.7 }}>
            <div>1. 点击「打开登录窗口」，Chrome 窗口会弹出</div>
            <div>2. 在窗口里完成 Pixiv 登录</div>
            <div>3. 登录成功后关闭浏览器窗口，状态自动更新</div>
            <div style={{ marginTop: 8 }}>
              <button className="mn-btn mn-btn-accent" disabled={pixivOpening}
                      onClick={() => {
                        setPixivOpening(true); setPixivMsg('');
                        fetch('/api/pixiv-open-login', { method: 'POST' })
                          .then(() => {
                            const poll = setInterval(() => {
                              fetch('/api/status').then(r => r.json()).then(s => {
                                if (s.pixiv_logged_in) { clearInterval(poll); setPixivOpening(false); onStatusReload && onStatusReload(); }
                              }).catch(() => {});
                            }, 3000);
                            setTimeout(() => { clearInterval(poll); setPixivOpening(false); }, 60000);
                          })
                          .catch(() => { setPixivOpening(false); setPixivMsg('请求失败'); });
                      }}
                      style={{ padding: '4px 12px', fontSize: 11 }}>
                {pixivOpening ? '正在打开…' : '打开登录窗口'}
              </button>
            </div>
          </div>
        )}
      </div>
      <div style={{ padding: '7px 0', borderBottom: `1px solid ${M.lineSoft}` }}>
        <div style={{ display: 'flex', alignItems: 'center' }}>
          <div style={{ fontSize: 12.5 }}>Civitai 账号</div>
          <div style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
            {civitaiMsg && <span className="mn-mono" style={{ fontSize: 10.5, color: M.red }}>{civitaiMsg}</span>}
            <span style={{ width: 6, height: 6, borderRadius: '50%', background: status.civitai_logged_in ? M.ok : M.inkFaint }} />
            <span className="mn-mono" style={{ fontSize: 11.5, color: M.inkDim }}>{status.civitai_logged_in ? '已登录' : '未设置'}</span>
            {status.civitai_logged_in ? (
              <button className="mn-btn mn-btn-ghost" disabled={civitaiSwitching}
                      onClick={() => {
                        setCivitaiSwitching(true); setCivitaiMsg('');
                        fetch('/api/civitai-logout', { method: 'POST' })
                          .then(r => r.json().then(d => ({ ok: r.ok, d })))
                          .then(({ ok, d }) => { setCivitaiSwitching(false); if (ok) { onStatusReload && onStatusReload(); } else setCivitaiMsg(d.error === 'civitai task is running' ? '停止当前任务后再切换' : d.error); })
                          .catch(() => { setCivitaiSwitching(false); setCivitaiMsg('请求失败'); });
                      }}
                      style={{ padding: '2px 8px', fontSize: 11 }}>
                {civitaiSwitching ? '…' : '切换账号'}
              </button>
            ) : (
              <button className="mn-btn mn-btn-ghost" onClick={() => setCivitaiGuideOpen(v => !v)}
                      style={{ padding: '2px 8px', fontSize: 11 }}>
                {civitaiGuideOpen ? '设置 ▴' : '设置 ▾'}
              </button>
            )}
          </div>
        </div>
        {!status.civitai_logged_in && civitaiGuideOpen && (
          <div style={{ marginTop: 8, fontSize: 11.5, color: M.inkDim, lineHeight: 1.7 }}>
            <div>1. 点击「打开登录窗口」，Chrome 窗口会弹出</div>
            <div>2. 在窗口里完成 Civitai 登录</div>
            <div>3. 登录成功后关闭浏览器窗口，状态自动更新</div>
            <div style={{ marginTop: 8 }}>
              <button className="mn-btn mn-btn-accent" disabled={civitaiOpening}
                      onClick={() => {
                        setCivitaiOpening(true); setCivitaiMsg('');
                        fetch('/api/civitai-open-login', { method: 'POST' })
                          .then(() => {
                            const poll = setInterval(() => {
                              fetch('/api/status').then(r => r.json()).then(s => {
                                if (s.civitai_logged_in) { clearInterval(poll); setCivitaiOpening(false); onStatusReload && onStatusReload(); }
                              }).catch(() => {});
                            }, 3000);
                            setTimeout(() => { clearInterval(poll); setCivitaiOpening(false); }, 60000);
                          })
                          .catch(() => { setCivitaiOpening(false); setCivitaiMsg('请求失败'); });
                      }}
                      style={{ padding: '4px 12px', fontSize: 11 }}>
                {civitaiOpening ? '正在打开…' : '打开登录窗口'}
              </button>
            </div>
          </div>
        )}
      </div>
      {/* X account */}
      <div style={{ padding: '7px 0', borderBottom: `1px solid ${M.lineSoft}` }}>
        <div style={{ display: 'flex', alignItems: 'center' }}>
          <div style={{ fontSize: 12.5 }}>X 账号</div>
          <div style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
            {xMsg && !status.x_logged_in && <span className="mn-mono" style={{ fontSize: 10.5, color: M.red }}>{xMsg}</span>}
            <span style={{ width: 6, height: 6, borderRadius: '50%', background: status.x_logged_in ? M.ok : M.inkFaint }} />
            <span className="mn-mono" style={{ fontSize: 11.5, color: M.inkDim }}>{status.x_logged_in ? '已登录' : '未设置'}</span>
            {status.x_logged_in ? (
              <button className="mn-btn mn-btn-ghost"
                      onClick={() => { setXMsg(''); fetch('/api/x-logout', { method: 'POST' }).then(() => onStatusReload && onStatusReload()).catch(() => setXMsg('请求失败')); }}
                      style={{ padding: '2px 8px', fontSize: 11 }}>切换账号</button>
            ) : (
              <button className="mn-btn mn-btn-ghost" onClick={() => setXGuideOpen(v => !v)}
                      style={{ padding: '2px 8px', fontSize: 11 }}>
                {xGuideOpen ? '设置 ▴' : '设置 ▾'}
              </button>
            )}
          </div>
        </div>
        {!status.x_logged_in && xGuideOpen && (
          <div style={{ marginTop: 8, fontSize: 11.5, color: M.inkDim, lineHeight: 1.7 }}>
            <div style={{ display: 'flex', gap: 0, marginBottom: 8, borderBottom: `1px solid ${M.lineSoft}` }}>
              {[['full', '扩展导出'], ['manual', 'F12 截取']].map(([key, label]) => (
                <button key={key} className="mn-btn mn-btn-ghost"
                        onClick={() => setXTabMethod(key)}
                        style={{ padding: '3px 10px', fontSize: 11, borderBottom: xTabMethod === key ? `2px solid ${M.accent}` : '2px solid transparent', borderRadius: 0, color: xTabMethod === key ? M.ink : M.inkDim }}>
                  {label}
                </button>
              ))}
            </div>
            {xTabMethod === 'full' ? (
              <>
                <div>1. 在浏览器登录 <span className="mn-mono" style={{ color: M.ink2 }}>x.com</span></div>
                <div>2. 安装 <span className="mn-mono" style={{ color: M.ink2 }}>Cookie-Editor</span> 或 <span className="mn-mono" style={{ color: M.ink2 }}>EditThisCookie</span>（Chrome / Edge）</div>
                <div>3. 点扩展图标 → Export → Export as JSON → 复制</div>
                <div>4. 粘贴完整 JSON 数组到下方，点保存</div>
                <div style={{ marginTop: 6, display: 'flex', gap: 6, alignItems: 'flex-start' }}>
                  <textarea className="mn-input" rows={3} value={xCookies} onChange={e => setXCookies(e.target.value)}
                            placeholder="粘贴 cookies JSON…"
                            style={{ flex: 1, fontSize: 11, fontFamily: M.mono, resize: 'vertical', minHeight: 54 }} />
                  <button className="mn-btn mn-btn-accent" disabled={xSaving || !xCookies.trim()}
                          onClick={() => {
                            setXSaving(true); setXMsg('');
                            fetch('/api/x-save-cookies', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ cookies: xCookies }) })
                              .then(r => r.json().then(d => ({ ok: r.ok, d })))
                              .then(({ ok, d }) => { setXSaving(false); if (ok) { setXCookies(''); onStatusReload && onStatusReload(); } else setXMsg(d.error || '保存失败'); })
                              .catch(() => { setXSaving(false); setXMsg('请求失败'); });
                          }}
                          style={{ padding: '4px 10px', fontSize: 11 }}>
                    {xSaving ? '…' : '保存'}
                  </button>
                </div>
              </>
            ) : (
              <>
                <div>1. 在浏览器登录 <span className="mn-mono" style={{ color: M.ink2 }}>x.com</span></div>
                <div>2. F12 → 应用 → Storage → Cookies → <span className="mn-mono" style={{ color: M.ink2 }}>https://x.com</span></div>
                <div>3. 找到 <span className="mn-mono" style={{ color: M.ink2 }}>auth_token</span> 和 <span className="mn-mono" style={{ color: M.ink2 }}>ct0</span>，复制 Value 列的值</div>
                <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 5 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span className="mn-mono" style={{ fontSize: 11, width: 75, flexShrink: 0, color: M.ink2 }}>auth_token</span>
                    <input className="mn-input" value={xAuthToken} onChange={e => setXAuthToken(e.target.value)}
                           placeholder="粘贴 auth_token 值"
                           style={{ flex: 1, fontSize: 11, fontFamily: M.mono, padding: '3px 6px' }} />
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span className="mn-mono" style={{ fontSize: 11, width: 75, flexShrink: 0, color: M.ink2 }}>ct0</span>
                    <input className="mn-input" value={xCt0} onChange={e => setXCt0(e.target.value)}
                           placeholder="粘贴 ct0 值"
                           style={{ flex: 1, fontSize: 11, fontFamily: M.mono, padding: '3px 6px' }} />
                  </div>
                  <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                    <button className="mn-btn mn-btn-accent" disabled={xSaving || !xAuthToken.trim() || !xCt0.trim()}
                            onClick={() => {
                              setXSaving(true); setXMsg('');
                              const cookies = JSON.stringify([
                                { name: 'auth_token', value: xAuthToken.trim(), domain: '.x.com', path: '/', httpOnly: true,  secure: true, sameSite: 'None' },
                                { name: 'ct0',        value: xCt0.trim(),       domain: '.x.com', path: '/', httpOnly: false, secure: true, sameSite: 'Lax'  },
                              ]);
                              fetch('/api/x-save-cookies', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ cookies }) })
                                .then(r => r.json().then(d => ({ ok: r.ok, d })))
                                .then(({ ok, d }) => { setXSaving(false); if (ok) { setXAuthToken(''); setXCt0(''); onStatusReload && onStatusReload(); } else setXMsg(d.error || '保存失败'); })
                                .catch(() => { setXSaving(false); setXMsg('请求失败'); });
                            }}
                            style={{ padding: '4px 10px', fontSize: 11 }}>
                      {xSaving ? '…' : '保存'}
                    </button>
                  </div>
                </div>
              </>
            )}
            {xMsg && <div style={{ marginTop: 4, fontSize: 11, color: M.red }}>{xMsg}</div>}
          </div>
        )}
      </div>
      {/* 小红书 account */}
      <div style={{ padding: '7px 0', borderBottom: `1px solid ${M.lineSoft}` }}>
        <div style={{ display: 'flex', alignItems: 'center' }}>
          <div style={{ fontSize: 12.5 }}>小红书账号</div>
          <div style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
            {xhsMsg && <span className="mn-mono" style={{ fontSize: 10.5, color: M.red }}>{xhsMsg}</span>}
            <span style={{ width: 6, height: 6, borderRadius: '50%', background: status.xhs_logged_in ? M.ok : M.inkFaint }} />
            <span className="mn-mono" style={{ fontSize: 11.5, color: M.inkDim }}>
              {status.xhs_logged_in ? '已登录' : '未登录'}
            </span>
            <button className="mn-btn mn-btn-ghost" onClick={() => setXhsGuideOpen(v => !v)}
                    style={{ padding: '2px 8px', fontSize: 11 }}>
              {xhsGuideOpen ? '说明 ▴' : '说明 ▾'}
            </button>
          </div>
        </div>
        {xhsGuideOpen && (
          <div style={{ marginTop: 8, fontSize: 11.5, color: M.inkDim, lineHeight: 1.7 }}>
            <div>发布时程序会自动启动 Chrome 并连接，首次需要在弹出的 Chrome 里登录小红书。</div>
            <div>登录一次后会记住，下次直接发布。</div>
            {!status.xhs_logged_in && (
              <div style={{ marginTop: 8 }}>
                <button className="mn-btn mn-btn-accent" disabled={xhsOpening}
                        onClick={() => {
                          setXhsOpening(true); setXhsMsg('');
                          fetch('/api/xhs-open-login', { method: 'POST' })
                            .then(() => {
                              const poll = setInterval(() => {
                                fetch('/api/status').then(r => r.json()).then(s => {
                                  if (s.xhs_logged_in) { clearInterval(poll); setXhsOpening(false); onStatusReload && onStatusReload(); }
                                }).catch(() => {});
                              }, 3000);
                              setTimeout(() => { clearInterval(poll); setXhsOpening(false); }, 60000);
                            })
                            .catch(() => { setXhsOpening(false); setXhsMsg('请求失败'); });
                        }}
                        style={{ padding: '4px 12px', fontSize: 11 }}>
                  {xhsOpening ? '正在打开…' : '提前登录'}
                </button>
                <span style={{ marginLeft: 8, fontSize: 10.5, color: M.inkFaint }}>或者直接上传，首次会弹窗让你登录</span>
              </div>
            )}
          </div>
        )}
      </div>
      <SetCompactRow label="上传队列" value={`${status.upload_count} 张`} />
      <div style={{ display: 'flex', alignItems: 'center', padding: '7px 0', borderBottom: `1px solid ${M.lineSoft}` }}>
        <div style={{ fontSize: 12.5 }}>自动调度</div>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
          <span className="mn-mono" style={{ fontSize: 11.5, color: sched.enabled ? M.ok : M.inkDim }}>
            {sched.enabled ? `下次：${fmtNextFire(sched.next_fire_at)} 后` : '关闭'}
          </span>
          {sched.enabled && (
            <button className="mn-btn mn-btn-ghost"
                    onClick={() => fetch('/api/scheduler', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: false }) })
                      .then(() => onStatusReload && onStatusReload())}
                    style={{ padding: '2px 8px', fontSize: 11 }}>
              停用
            </button>
          )}
          <button className="mn-btn mn-btn-ghost" onClick={onSchedulerConfigure} style={{ padding: '2px 8px', fontSize: 11 }}>配置</button>
        </div>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', padding: '7px 0', borderBottom: `1px solid ${M.lineSoft}` }}>
        <div style={{ fontSize: 12.5 }}>API key</div>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 6, alignItems: 'center' }}>
          <span style={{ width: 6, height: 6, borderRadius: '50%', background: status.has_api_key ? M.ok : M.red }} />
          <span className="mn-mono" style={{ fontSize: 11.5, color: M.inkDim }}>
            {status.has_api_key ? (status.api_key_masked || '已设置') : '未设置'}
          </span>
        </div>
      </div>
      <div style={{ paddingTop: 8, display: 'flex', gap: 6 }}>
        <input className="mn-input" placeholder="粘贴 Civitai API key…" type="password"
               value={apiKey} onChange={e => setApiKey(e.target.value)}
               onKeyDown={e => e.key === 'Enter' && saveKey()}
               style={{ flex: 1, fontSize: 12 }} />
        <button className="mn-btn mn-btn-accent"
                onClick={saveKey} disabled={!apiKey.trim()}
                style={{ padding: '6px 12px', fontSize: 12, opacity: apiKey.trim() ? 1 : 0.5 }}>
          {saved ? '✓' : '保存'}
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
