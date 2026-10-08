import { redirect } from "next/navigation";

export default function OutreachRedirect() {
  redirect("/admin/email-agent/status");
}
