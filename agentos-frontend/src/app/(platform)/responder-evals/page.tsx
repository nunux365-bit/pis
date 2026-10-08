import { redirect } from "next/navigation";
import { RESPONDER_EVAL_DEFAULT_PATH } from "@/lib/responderNav";

export default function ResponderEvalsIndexPage() {
  redirect(RESPONDER_EVAL_DEFAULT_PATH);
}
