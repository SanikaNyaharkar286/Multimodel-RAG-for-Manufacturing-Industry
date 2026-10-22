import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useEffect, useRef, useState } from 'react';
import { firebaseAuth } from '../../auth/firebase';
import { logPerformance } from '../../utils/performanceLog';
import RepairControls from './RepairControls';
import MaintenanceReport from './MaintenanceReport';

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
  useEffect(() => {
    let active = true;
    let objectUrl = null;
    (async () => {
      try {
        const token = firebaseAuth?.currentUser ? await firebaseAuth.currentUser.getIdToken() : null;
        const response = await fetch(block.src, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
        if (!response.ok) throw new Error(`image-${response.status}`);
        objectUrl = URL.createObjectURL(await response.blob());
        if (active) setSrc(objectUrl);
      } catch {
        if (active) setFailed(true);
        logPerformance('answer-image-error', { request_id: requestId, block_index: index });
      }
    })();
    return () => { active = false; if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [block.src, index, requestId]);
  if (failed) return <p>Image unavailable or session expired.</p>;
  if (!src) return <p>Loading manual image…</p>;
  return <img src={src} alt={block.caption || "Original manual diagram"} loading="lazy" onLoad={() => logPerformance('answer-image-loaded', { request_id: requestId, block_index: index, observed_at_ms: performance.now() })} />;
}

function renderBlock(block, index, requestId) {
  switch (block.type) {
    case "text":
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
            {block.description && (
              <div className="image-description">
                <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml disallowedElements={["img"]}>
                  {block.description}
                </ReactMarkdown>
              </div>
            )}
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
  if (!response) return null;

  return (
    <div className="response agent-response">
      {showSafetyWarning && response.safety_warning && <div className="repair-safety" role="alert">
        <strong>Safety before repair</strong>
        <p>{response.safety_warning}</p>
      </div>}
      {(response.blocks || []).map((block, index) => renderBlock(block, index, response.request_id))}
      {response.maintenance_report && <MaintenanceReport report={response.maintenance_report} />}
      {repairActive && onRepairAction && <RepairControls
        key={`${response.repair_workflow.workflow_id}:${response.repair_workflow.current_step?.step_id || 'finish'}`}
        workflow={response.repair_workflow} loading={loading} onAction={onRepairAction} />}

    </div>
  );
}
