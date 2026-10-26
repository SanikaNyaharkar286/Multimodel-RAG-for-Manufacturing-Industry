import "./styles/global.css";
import "./styles/chat.css";
import "./styles/profile.css";

import useChat from "./hooks/useChat";
import Header from "./components/layout/Header";
import Welcome from "./components/layout/Welcome";
import ChatInput from "./components/chat/ChatInput";
import ChatThread from "./components/chat/ChatThread";
import { AuthGate } from "./auth/AuthGate";

function ChatWorkspace() {
  const chat = useChat();
  const hasMessages = chat.messages.length > 0;

  const input = (
    <ChatInput
      question={chat.question}
      setQuestion={chat.setQuestion}
      image={chat.image}
      error={chat.error}
      loading={chat.loading}
      compact={hasMessages}
      onImageSelect={chat.selectImage}
      onImageRemove={chat.removeImage}
      onSend={chat.sendMessage}
    />
  );

  function focusChat() {
    const composer = document.getElementById('chat-question');
    composer?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    composer?.focus({ preventScroll: true });
  }

  function openCurrentReport() {
    const reports = document.querySelectorAll('.maintenance-report');
    const documents = document.querySelectorAll('.documentation-actions');
    const target = reports[reports.length - 1] || documents[documents.length - 1];
    if (!target) return false;
    target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    target.focus({ preventScroll: true });
    return true;
  }

  return (
    <div className={`app ${hasMessages ? "app--chat" : ""}`}>
      <div className="chat-layout">
        <Header onNewChat={chat.resetChat} showNewChat={hasMessages}
          onChatOpen={focusChat} onReportsOpen={openCurrentReport} />
      {hasMessages ? (
        <>
          <ChatThread messages={chat.messages} loading={chat.loading}
            repairWorkflow={chat.repairWorkflow} onRepairAction={chat.sendRepairAction} onRetry={chat.retryMessage} onRefresh={chat.refreshChat} />
          <div className="input-dock">
            <div className="dock-inner">{input}</div>
          </div>
        </>
      ) : (
        <main className="main-container">
          <Welcome onExampleSelect={chat.setQuestion}>{input}</Welcome>
        </main>
      )}
      </div>
    </div>
  );
}

function App() {
  return <AuthGate><ChatWorkspace /></AuthGate>;
}

export default App;
