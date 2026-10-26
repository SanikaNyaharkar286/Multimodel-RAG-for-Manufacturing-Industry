import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useEffect, useRef, useState } from 'react';
import { firebaseAuth } from '../../auth/firebase';
import { APP_CONFIG } from '../../config/appConfig';
import { logPerformance } from '../../utils/performanceLog';
import RepairControls from './RepairControls';
import MaintenanceReport from './MaintenanceReport';
import ManufacturerSupport from './ManufacturerSupport';
import Documentation from './Documentation';
import useDialogFocus from '../../hooks/useDialogFocus';

import "../../styles/agent-response.css";


function safeLink(value) {
  try {
    const url = new URL(value);

    return ["http:", "https:"].includes(url.protocol)
      ? url.href
      : null;
  } catch {
    return null;
  }
}


function MarkdownTable({ children }) {
  return (
    <div className="table-wrapper">
      <table>{children}</table>
    </div>
  );
}


function AuthenticatedImage({ block, index, requestId }) {
  const [src, setSrc] = useState(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [expanded, setExpanded] = useState(false);
  const dialogRef = useDialogFocus(expanded, () => setExpanded(false));
  useEffect(() => {
    let active = true;
    let objectUrl = null;
    (async () => {
      try {
        const imageUrl = block.src.startsWith('http')
          ? block.src
          : `${APP_CONFIG.apiBaseUrl}${block.src}`;
        const currentUser = firebaseAuth?.currentUser;
        let token = currentUser ? await currentUser.getIdToken() : null;
        let response = await fetch(imageUrl, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
        if (response.status === 401 && currentUser) {
          token = await currentUser.getIdToken(true);
          response = await fetch(imageUrl, { headers: { Authorization: `Bearer ${token}` } });
        }
        if (!response.ok) throw new Error(`image-${response.status}`);
        objectUrl = URL.createObjectURL(await response.blob());
        if (active) setSrc(objectUrl);
      } catch {
        if (active) setFailed(true);
        logPerformance('answer-image-error', { request_id: requestId, block_index: index });
      }
    })();
    return () => { active = false; if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [block.src, index, requestId, attempt]);
  if (failed) return <div className="image-retry"><p>Could not load this manual image.</p><button type="button" onClick={() => { setFailed(false); setSrc(null); setAttempt(value => value + 1); }}>Retry image</button></div>;
  if (!src) return <p>Loading manual image…</p>;
  return <><button className="manual-image-button" type="button" onClick={() => setExpanded(true)} aria-label="Enlarge manual image"><img src={src} alt={block.caption || "Original manual diagram"} loading="lazy" onLoad={() => logPerformance('answer-image-loaded', { request_id: requestId, block_index: index, observed_at_ms: performance.now() })} /></button>
    {expanded && <div ref={dialogRef} tabIndex={-1} className="image-lightbox" role="dialog" aria-modal="true" aria-label="Manual image preview"><button type="button" onClick={() => setExpanded(false)}>Close preview</button><img src={src} alt={block.caption || 'Manual diagram'} /></div>}</>;
}

function renderBlock(block, index, requestId) {
  switch (block.type) {
    case "text":
      // Hide the verbose source appendix stored in older conversations. New
      // responses use the concise, deduplicated structured source list below.
      if (/^\s*#{1,6}\s+Sources\s*(?:\r?\n|$)/i.test(block.content || "")) return null;
      return (
        <div
          className="response-text markdown-response"
          key={index}
        >
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            skipHtml
            disallowedElements={["img"]}
            components={{
              table: MarkdownTable,
            }}
          >
            {block.content || ""}
          </ReactMarkdown>
        </div>
      );

    case "image":
      return (
        <figure
          className="response-figure"
          key={index}
        >
          <AuthenticatedImage block={block} index={index} requestId={requestId} />

          <figcaption>
            {block.caption && <strong>{block.caption}</strong>}
            {block.source && <p className="manual-source">{block.source.document_name} · PDF page {block.source.page_number}</p>}
          </figcaption>
        </figure>
      );

    case "table":
      return (
        <div
          className="response-table"
          key={index}
        >
          {block.title && (
            <h3>{block.title}</h3>
          )}

          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  {(block.columns || []).map(
                    (column, columnIndex) => (
                      <th key={columnIndex}>
                        {column}
                      </th>
                    )
                  )}
                </tr>
              </thead>

              <tbody>
                {(block.rows || []).map(
                  (row, rowIndex) => (
                    <tr key={rowIndex}>
                      {row.map(
                        (cell, cellIndex) => (
                          <td key={cellIndex}>
                            {cell}
                          </td>
                        )
                      )}
                    </tr>
                  )
                )}
              </tbody>
            </table>
          </div>
        </div>
      );

    case "link": {
      const url = safeLink(block.url);

      if (!url) return null;

      return (
        <p
          className="response-video-link"
          key={index}
        >
          <a
            href={url}
            target="_blank"
            rel="noopener noreferrer"
          >
            {block.title ||
              (block.isVideo
                ? "Watch related video"
                : "Open manual reference")}
          </a>
        </p>
      );
    }

    default:
      return null;
  }
}


export default function ResponseRenderer({
  response,
  loading,
  repairActive,
  onRepairAction,
  showSafetyWarning = true,
  documentationMode = false,
  allowDocumentation = true,
}) {
  const loggedResponse = useRef(null);
  useEffect(() => {
    if (!response?.client_timing || loggedResponse.current === response) return;
    const frame = requestAnimationFrame(() => {
      loggedResponse.current = response;
      logPerformance('answer-committed', {
        request_id: response.request_id,
        response_to_frame_ms: performance.now()-response.client_timing.response_received_at,
        block_count: response.blocks?.length || 0,
      });
    });
    return () => cancelAnimationFrame(frame);
  }, [response]);
  useEffect(() => {
    if (!response?.repair_workflow?.current_step) return;
    logPerformance('repair-step-displayed', {
      request_id: response.request_id,
      step_number: response.repair_workflow.current_step.number,
      displayed_at_ms: performance.now(),
    });
  }, [response]);
  // Old saved conversations may contain the retired case-confirmation notice.
  if (!response || response.case_conflict) return null;

  return (
    <div className="response agent-response" id={repairActive && !documentationMode ? 'active-repair-step' : undefined}>
      {showSafetyWarning && response.safety && <div className="repair-safety" role="alert"><strong>⚠ {response.safety.title}</strong><ul>{response.safety.bullets.map(item => <li key={item}>{item}</li>)}</ul></div>}
      {showSafetyWarning && !response.safety && response.safety_warning && <div className="repair-safety" role="alert">
        <strong>Safety before repair</strong>
        <p>{response.safety_warning}</p>
      </div>}
      {response.plan_updated && <p className="plan-update" role="status">The remaining steps have been updated using your new details. Completed actions are preserved.</p>}
      {response.repair_workflow?.current_step && <div className="step-summary"><span className="step-progress">Step {response.repair_workflow.current_step.number}{response.repair_workflow.total_steps ? ` of ${response.repair_workflow.total_steps}` : ''}{response.repair_workflow.conditional ? ' · depends on observations' : ''}</span>
        {response.repair_workflow.current_step.title && <h3>{response.repair_workflow.current_step.title}</h3>}</div>}
      {(response.blocks || []).map((block, index) => renderBlock(
        response.repair_workflow?.current_step && block.type === 'text'
          ? {...block, content:(block.content || '').replace(/^### Step \d+\s*\n/, '')} : block,
        index, response.request_id))}
      {response.repair_workflow?.current_step?.expected_result && <p className="expected-result"><strong>Expected observation:</strong> {response.repair_workflow.current_step.expected_result}</p>}
      {response.repair_workflow?.current_step?.precautions?.length > 0 && <aside className="step-precautions"><strong>Step precautions</strong><ul>{response.repair_workflow.current_step.precautions.map(text => <li key={text}>{text}</li>)}</ul></aside>}
      {['awaiting_resolution', 'resolved', 'unresolved'].includes(response.repair_workflow?.status)
        && <ManufacturerSupport support={response.repair_workflow.manufacturer_support} />}
      {response.sources?.length > 0 && <details className="manual-references"><summary>Source</summary><ul>{Array.from(new Map(response.sources.map(source => [`${source.document_name}:${source.page_number}`, source])).values()).map(source => <li key={`${source.document_name}:${source.page_number}`}>{source.document_name} · page {source.page_number}</li>)}</ul></details>}
      {!documentationMode && !response.documentation && response.maintenance_report && <MaintenanceReport report={response.maintenance_report} />}
      {!documentationMode && allowDocumentation && response.documentable !== false
        && response.request_type !== 'smalltalk'
        && (!response.repair_workflow
          || ['resolved', 'unresolved'].includes(response.repair_workflow.status))
        && (response.documentation || (response.session_id && response.message_id))
        && <Documentation response={response}/>}
      {!documentationMode && repairActive && onRepairAction && <RepairControls
        key={`${response.repair_workflow.workflow_id}:${response.repair_workflow.current_step?.step_id || 'finish'}`}
        workflow={response.repair_workflow} loading={loading} onAction={onRepairAction} />}

    </div>
  );
}
