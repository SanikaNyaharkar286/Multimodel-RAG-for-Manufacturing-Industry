import { useEffect, useRef } from "react";
import ChatMessage from "./ChatMessage";

function ChatThread({ messages, loading, repairWorkflow, onRepairAction }) {
  const bottomRef = useRef(null);
  const activeMessage = messages.findLast(message => message.role === 'assistant'
    && message.status === 'done');
  const firstRepairMessage = messages.find(message => message.role === 'assistant'
    && message.status === 'done' && message.response?.safety_warning);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  return (
    <div className="chat-thread">
      {messages.map((message) => (
        <ChatMessage key={message.id} message={message} loading={loading}
          repairActive={Boolean(repairWorkflow && message.response?.repair_workflow)
            && message.id === activeMessage?.id
            && repairWorkflow?.status !== 'resolved'
            && message.response?.repair_workflow?.workflow_id === repairWorkflow?.workflow_id
            && message.response?.repair_workflow?.current_step?.step_id === repairWorkflow?.current_step?.step_id}
          showSafetyWarning={message.id === firstRepairMessage?.id}
          onRepairAction={onRepairAction} />
      ))}
      <div ref={bottomRef} />
    </div>
  );
}

export default ChatThread;
