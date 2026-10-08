import { redirect } from "next/navigation";

/** Legacy path — finance requisition lives at ``/s2p/procurement``. */
export default function ProcurementHubRedirectPage() {
  redirect("/s2p/procurement");
}
