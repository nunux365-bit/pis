import { Suspense } from "react";
import { ChatExperience } from "./ChatExperience";

export default function ChatPage() {
  return (
    <Suspense
      fallback={
        <div className="flex h-[50vh] items-center justify-center text-sm text-[var(--text-muted)]">
          Loading chat…
        </div>
      }
    >
      <ChatExperience />
    </Suspense>
  );
}
