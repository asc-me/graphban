import { useSearchParams } from "react-router-dom";

import { fleetTabFromSearch } from "./FleetPageTabs";
import { FleetV2View } from "./FleetV2View";
import { FleetView } from "./FleetView";

/** The one Fleet page: the roster or the harness catalog, chosen by `?tab=`. */
export function FleetPage() {
  const [params] = useSearchParams();
  return fleetTabFromSearch(params) === "harness" ? <FleetV2View /> : <FleetView />;
}
