import { redirect } from "next/navigation";
import { WORKFLOW_DEFAULT_PATH } from "@/lib/workflowNav";

export default function O2CIndexPage() {
  redirect(WORKFLOW_DEFAULT_PATH);
}
