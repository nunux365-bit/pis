import { redirect } from "next/navigation";

export default function EmailAgentIndexPage() {
  redirect("/admin/email-agent/receivables");
}
