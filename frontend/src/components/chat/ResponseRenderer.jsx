import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Fragment, useEffect, useRef, useState } from 'react';
import { cachedManualImage, loadManualImage } from '../../services/manualImageCache';
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


function publicImageDescription(value) {
  return String(value || '')
    .replace(/\bmanual context\s*:\s*/ig, '')
    .replace(/\b(?:image\s*id|chunk\s*id|source\s*id)\s*[:#]?\s*[a-z0-9_.-]+/ig, '')
    .replace(/[^\S\r\n]{2,}/g, ' ')
    .trim();
}

// Normalize old and new model descriptions into one universal image format.
function imageDetails(value) {
  const lines = String(value || '')
    .replace(/\r/g, '')
    .split('\n')
    .map(line => line.replace(/^\s*[-*•]\s*/, '').replace(/\*\*/g, '').trim())
    .filter(Boolean);
  const description = [];
  const callouts = [];
  const checks = [];

  for (const line of lines) {
    if (/^(?:source|page|type|relevance|manual information)\s*:/i.test(line)) continue;
    const callout = line.match(/^callout\s*(\d+)\s*[—:-]\s*(.+)$/i);
    if (callout) {
      callouts.push(`Callout ${callout[1]} — ${callout[2].trim()}`);
      continue;
    }
    const check = line.match(/^(?:technician check|inspection|inspect|verify)\s*:\s*(.+)$/i);
    if (check) {
      checks.push(check[1].trim());
      continue;
    }
    const labeledDescription = line.match(/^description\s*:\s*(.+)$/i);
    if (labeledDescription) {
      description.push(labeledDescription[1].trim());
      continue;
    }
    description.push(line);
  }

  return {
    description: [...new Set(description)].join(' '),
    callouts: [...new Set(callouts)],
    check: [...new Set(checks)].join(' '),
  };
}


function MarkdownTable({ children }) {
  return (
    <div className="table-wrapper">
      <table>{children}</table>
    </div>
  );
}


function AuthenticatedImage({ block, index, requestId }) {
  const imageUrl = block.src.startsWith('http') ? block.src : `${APP_CONFIG.apiBaseUrl}${block.src}`;
  const [src, setSrc] = useState(() => cachedManualImage(imageUrl));
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [expanded, setExpanded] = useState(false);
  const dialogRef = useDialogFocus(expanded, () => setExpanded(false));
  useEffect(() => {
    let active = true;
    setSrc(cachedManualImage(imageUrl));
    (async () => {
      try {
        const objectUrl = await loadManualImage(imageUrl, attempt > 0);
        if (active) setSrc(objectUrl);
      } catch {
        if (active) setFailed(true);
        logPerformance('answer-image-error', { request_id: requestId, block_index: index });
      }
    })();
    return () => { active = false; };
  }, [imageUrl, index, requestId, attempt]);
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
      {
      const details = imageDetails(publicImageDescription(block.description));
      return (
        <figure
          className="response-figure"
          key={index}
        >
          <AuthenticatedImage block={block} index={index} requestId={requestId} />

          <figcaption>
            {block.caption && <strong>{block.caption}</strong>}
            {(details.description || details.callouts.length || details.check) && <ul className="manual-image-details">
              {details.description && <li><strong>Description:</strong> {details.description}</li>}
              {details.callouts.map(callout => <li key={callout}><strong>{callout}</strong></li>)}
              {details.check && <li><strong>Technician check:</strong> {details.check}</li>}
            </ul>}
            {block.source && <p className="manual-source">{block.source.document_name} · PDF page {block.source.page_number}</p>}
          </figcaption>
        </figure>
      );
      }

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
  const blocks = response?.blocks || [];
  const reportWorkflow = response?.repair_workflow || response?.documentation?.workflow;
  const repairInProgress = reportWorkflow
    && !['resolved', 'unresolved'].includes(reportWorkflow.status)
    && Boolean(reportWorkflow.status || reportWorkflow.current_step);
  const guidedStep = Boolean(response?.repair_workflow?.current_step);
  const firstImage = guidedStep ? blocks.findIndex(block => block.type === 'image') : -1;
  const verification = response?.repair_workflow?.current_step?.expected_result
    ? <section className="step-verification" aria-label="How to verify this step"><strong>How to verify</strong><p className="expected-result"><strong>Expected observation:</strong> {response.repair_workflow.current_step.expected_result}</p></section>
    : null;
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
      {response.repair_workflow?.current_step?.precautions?.length > 0 && <aside className="step-precautions"><strong>Step precautions</strong><ul>{response.repair_workflow.current_step.precautions.map(text => <li key={text}>{text}</li>)}</ul></aside>}
      {blocks.map((block, index) => <Fragment key={index}>
        {index === firstImage && verification}
        {renderBlock(
        response.repair_workflow?.current_step && block.type === 'text'
          ? {...block, content:(block.content || '').replace(/^### Step \d+\s*\n/, '')} : block,
        index, response.request_id)}
      </Fragment>)}
      {firstImage < 0 && verification}
      {['awaiting_resolution', 'resolved', 'unresolved'].includes(response.repair_workflow?.status)
        && <ManufacturerSupport support={response.repair_workflow.manufacturer_support} />}
      {response.sources?.length > 0 && <details className="manual-references"><summary>Source</summary><ul>{Array.from(new Map(response.sources.map(source => [`${source.document_name}:${source.page_number}`, source])).values()).map(source => <li key={`${source.document_name}:${source.page_number}`}>
        <span>{source.document_name} · page {source.page_number}</span>
        {source.links?.length > 0 && <ul className="manual-source-links">{source.links.map((reference, index) => {
          const url = safeLink(reference.url);
          return url ? <li key={`${url}-${index}`}><a href={url} target="_blank" rel="noopener noreferrer">{reference.title || (reference.is_video ? 'Watch related video' : 'Open original manual reference')}</a></li> : null;
        })}</ul>}
      </li>)}</ul></details>}
      {!documentationMode && !response.documentation && response.maintenance_report && <MaintenanceReport report={response.maintenance_report} />}
      {!documentationMode && allowDocumentation && !loading && !repairInProgress && response.documentable !== false
        && response.request_type !== 'smalltalk'
        && (response.documentation || (response.session_id && response.message_id))
        && <Documentation response={response}/>}
      {!documentationMode && repairActive && onRepairAction && <RepairControls
        key={`${response.repair_workflow.workflow_id}:${response.repair_workflow.current_step?.step_id || 'finish'}`}
        workflow={response.repair_workflow} loading={loading} onAction={onRepairAction} />}

    </div>
  );
}
