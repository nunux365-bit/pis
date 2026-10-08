export const RESPONDER_EVAL_DEFAULT_PATH = "/responder-evals/chat-eval" as const;

export const RESPONDER_PROGRAMS = [
  { href: RESPONDER_EVAL_DEFAULT_PATH, label: "Chat Eval" },
  { href: "/responder-evals/hand-off-analysis", label: "Hand-Off Analysis" },
  { href: "/responder-evals/jit-hold", label: "JIT Hold" },
] as const;
