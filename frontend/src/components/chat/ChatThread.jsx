import { useEffect, useRef } from "react";
import ChatMessage from "./ChatMessage";

function ChatThread({ messages, loading, repairWorkflow, onRepairAction, onRetry, onRefresh }) {
  const bottomRef = useRef(null);
  const activeMessage = messages.findLast(message => message.role === 'assistant'
    && message.status === 'done'
    && message.response?.repair_workflow?.workflow_id === repairWorkflow?.workflow_id
    && message.response?.repair_workflow?.current_step?.step_id === repairWorkflow?.current_step?.step_id
    && (!repairWorkflow?.current_step || message.response?.blocks?.some(block => block.type === 'text' && block.content?.includes('### Step '))));
  const firstSafetyByCase = new Map();
  messages.forEach(message => {
    if (message.role !== 'assistant' || message.status !== 'done' || !(message.response?.safety || message.response?.safety_warning)) return;
    const caseId = message.response?.repair_workflow?.workflow_id || 'legacy';
    if (!firstSafetyByCase.has(caseId)) firstSafetyByCase.set(caseId, message.id);
  });

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  return (
    <div className="chat-thread">
      {messages.map((message) => {
        const done = repairWorkflow?.completed_steps?.some(step => step.step_id === message.response?.repair_workflow?.current_step?.step_id);
        const body = <ChatMessage key={message.id} message={message} loading={loading}
          repairActive={Boolean(repairWorkflow && message.response?.repair_workflow)
            && message.id === activeMessage?.id
            && !['resolved', 'unresolved'].includes(repairWorkflow?.status)
            && message.response?.repair_workflow?.workflow_id === repairWorkflow?.workflow_id
            && message.response?.repair_workflow?.current_step?.step_id === repairWorkflow?.current_step?.step_id}
          showSafetyWarning={message.id === firstSafetyByCase.get(message.response?.repair_workflow?.workflow_id || 'legacy')}
          onRetry={onRetry} onRefresh={onRefresh} onRepairAction={onRepairAction} />;
        return done ? <details className="completed-step" key={message.id}><summary>✓ Step {message.response.repair_workflow.current_step.number} completed</summary>{body}</details> : body;
      })}
      <div ref={bottomRef} />
    </div>
  );
}

export default ChatThread;
