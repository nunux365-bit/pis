import { readFile } from "node:fs/promises";
import path from "node:path";
import { cache } from "react";
import PnlDashboardClient, { type PnlData } from "./PnlDashboardClient";

/** Read the (large) P&L dataset from disk once per server process instead of
 *  bundling it into the build. `cache` dedupes within a request. */
const loadPnlData = cache(async (): Promise<PnlData> => {
  const file = path.join(process.cwd(), "src", "static", "pnl_data.json");
  return JSON.parse(await readFile(file, "utf8")) as PnlData;
});

export default async function PnlDashboardPage() {
  const data = await loadPnlData();
  return <PnlDashboardClient data={data} />;
}
