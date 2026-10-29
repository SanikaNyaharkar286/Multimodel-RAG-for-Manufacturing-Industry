import { useEffect, useRef, useState } from 'react';
import { validateImage } from '../../utils/validateImage';

export default function RepairControls({ workflow, loading, onAction }) {
  const [reporting, setReporting] = useState(false);
  const [text, setText] = useState('');
  const [image, setImage] = useState(null);
  const [error, setError] = useState('');
  const [observation, setObservation] = useState('');
  const completion = 'done';
  const setCompletion = () => {};
  const [confirmed, setConfirmed] = useState(false);
  const imageRef = useRef(null);
  useEffect(() => () => { if (imageRef.current) URL.revokeObjectURL(imageRef.current); }, []);
  useEffect(() => {
    setObservation('');
    setConfirmed(false);
  }, [workflow.current_step?.step_id]);
  const identity = { workflow_id: workflow.workflow_id, step_id: workflow.current_step?.step_id || '', plan_version: workflow.plan_version || 1 };
  const choices = workflow.current_step?.branches || [];
  function choose(event) {
    const file = event.target.files?.[0]; event.target.value = '';
    if (!file) return;
    const result = validateImage(file);
    if (!result.valid) { setError(result.error); return; }
    if (imageRef.current) URL.revokeObjectURL(imageRef.current);
    const url = URL.createObjectURL(file); imageRef.current = url;
    setImage({file, previewUrl:url}); setError('');
  }
  if (reporting) return <form className="repair-issue-form" onSubmit={event => {
    event.preventDefault(); if (!loading && (text.trim() || image)) {
      onAction({...identity, action:'report_issue', issue_report:text.trim() || 'Please assess the attached observation.', imageFile:image?.file, imagePreviewUrl:image?.previewUrl});
      setReporting(false);
    }
  }}><h3>Add details to this step</h3><p>Your observations stay attached to this case. The step will not be marked complete.</p>
    <label>Question or observation<textarea value={text} onChange={event => setText(event.target.value)} maxLength={2000} disabled={loading} placeholder="Describe what you see or ask for clarification."/></label>
    <label>Upload image<input type="file" accept="image/png,image/jpeg,image/webp" onChange={choose} disabled={loading}/></label>
    {image && <div className="repair-issue-image-preview"><img src={image.previewUrl} alt="Current step observation"/><button type="button" onClick={() => {URL.revokeObjectURL(imageRef.current); imageRef.current=null; setImage(null);}}>Remove image</button></div>}
    {error && <p role="alert">{error}</p>}<div className="repair-actions"><button type="submit" disabled={loading || (!text.trim() && !image)}>Send details</button><button type="button" disabled={loading} onClick={() => setReporting(false)}>Cancel</button></div>
  </form>;
  return <div className="repair-controls">
    {workflow.status === 'awaiting_done' && <>
      <p>Done records this action as completed. Select Issue Resolved only when you have observed resolution.</p>
      <label className="observation-choice" style={{display:'none'}}>Did you complete this step?
        <select value={completion} onChange={event => setCompletion(event.target.value)} aria-disabled={loading}>
          <option value="">Select an outcome</option>
          <option value="done">Yes — the step is complete</option>
          <option value="not_done">No — I could not complete the step</option>
        </select>
      </label>
      {completion === 'not_done' && <p role="status">Use “Add Details / Upload Image” to report what prevented completion. The step will remain open.</p>}
      {choices.length > 0 && <label className="observation-choice">{workflow.current_step.observation_prompt || 'What did you observe?'}<select value={observation} onChange={event => setObservation(event.target.value)} disabled={loading}><option value="">Select the observed result</option>{choices.map(choice => <option key={choice.label}>{choice.label}</option>)}</select></label>}
      <label className="safety-confirmation"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} disabled={loading}/>I confirm I followed the applicable isolation and safety procedure for this step.</label>
      <button type="button" className="repair-next" disabled={loading || completion !== 'done' || !confirmed || (choices.length > 0 && !observation)} onClick={() => onAction({...identity, action:'done', observation, safety_confirmed:confirmed})}>{loading ? 'Saving…' : 'Done — Next Step'}</button>
    </>}
    <button type="button" className="repair-resolve" disabled={loading} onClick={() => onAction({...identity, action:'resolve'})}>Issue Resolved</button>
    <button type="button" className="repair-details" disabled={loading} onClick={() => setReporting(true)}>Add Details / Upload Image</button>
    {workflow.status === 'awaiting_resolution' && <><p>The prepared procedure has ended. Record the actual result or add evidence for reassessment.</p><button type="button" className="repair-unresolved" disabled={loading} onClick={() => onAction({...identity, action:'unresolved'})}>Still Not Resolved — Create Report</button></>}
  </div>;
}
