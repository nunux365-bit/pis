import { redirect } from "next/navigation";

/** Legacy path — access is restricted to admin + GLP compliance reviewer on `/compliance/call-quality`. */
export default function LegacyCallQualityRedirect() {
  redirect("/compliance/call-quality");
}
