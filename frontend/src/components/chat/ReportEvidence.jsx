import { useEffect, useState } from 'react';
import { documentRequest } from '../../services/documentationApi';
import { reportImagePath } from './reportReview';

export function ReportImage({ reportId, evidence, number }) {
  const [src, setSrc] = useState('');
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [expanded, setExpanded] = useState(false);
  useEffect(() => {
    let active = true;
    let objectUrl;
    documentRequest(reportImagePath(reportId, evidence.image_id), null, true).then(blob => {
      if (!active) return;
      objectUrl = URL.createObjectURL(blob);
      setSrc(objectUrl);
    }).catch(() => { if (active) setFailed(true); });
    return () => { active = false; if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [reportId, evidence.image_id, attempt]);
  return <figure className={`report-evidence-card${expanded ? ' report-evidence-card--expanded' : ''}`}>
    {failed ? <div role="alert"><p>Evidence image unavailable. The reference is retained.</p><button type="button" onClick={() => { setSrc(''); setFailed(false); setAttempt(value => value + 1); }}>Retry image</button></div>
      : src ? <button type="button" className="report-image-preview" aria-expanded={expanded} aria-label={`${expanded ? 'Reduce' : 'Enlarge'} ${evidence.caption || `evidence image ${number}`}`} onClick={() => setExpanded(value => !value)}><img src={src} alt={evidence.caption || evidence.description || `Evidence image ${number}`} onError={() => setFailed(true)} /></button>
        : <p role="status">Loading evidence image…</p>}
    <figcaption><strong>Image {number}: {evidence.caption || 'Supporting evidence'}</strong>
      {evidence.description && <p>{evidence.description}</p>}
      <p>Source type: {evidence.source_type || 'Not recorded'}{evidence.step_id && ` · Step reference: ${evidence.step_id}`}</p>
      {evidence.source && <p>{typeof evidence.source === 'string' ? evidence.source
        : [evidence.source.document_name, evidence.source.page_number != null ? `PDF page ${evidence.source.page_number}` : ''].filter(Boolean).join(' · ')}</p>}
      <small>Evidence reference: {evidence.image_id}</small>
    </figcaption>
  </figure>;
}

export default function ReportEvidence({ report }) {
  const evidence = (report.evidence || []).filter(item => item.image_id);
  return evidence.length ? <div className="report-evidence-grid">{evidence.map((item, index) => <ReportImage key={`${report.report_id}-${item.image_id}-${index}`} reportId={report.report_id} evidence={item} number={index + 1} />)}</div>
    : <p>No photographic evidence attached.</p>;
}
