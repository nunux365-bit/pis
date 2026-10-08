import { redirect } from "next/navigation";

/** Old path; programs no longer list purchase orders here. */
export default function S2PPurchaseOrdersRedirectPage() {
  redirect("/s2p/procurement");
}
