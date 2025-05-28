// contexts/BLEContext.tsx
import React, {
  createContext,
  useContext,
  useState,
  type ReactNode,
} from "react";

import { BleError, Characteristic } from "react-native-ble-plx";
import { useBLE } from "@/hooks/useBLE";
import { decode } from "@/utils/ble_codec";
import { MESSAGE_TYPES } from "@/utils/ble_constants";

/* ------------------------------------------------------------------ */
/*  1.  context type = all fields from useBLE  +  piStatus            */
/* ------------------------------------------------------------------ */
type BLECtx = ReturnType<typeof useBLE> & {
  /** payload last pushed by the Raspberry Pi (e.g. {connected:[…]}) */
  piStatus: any;
  dbUpdateTrigger: number;
  triggerDbUpdate: () => void;
};

const Ctx = createContext<BLECtx | null>(null);

/* ------------------------------------------------------------------ */
/*  2.  provider                                                      */
/* ------------------------------------------------------------------ */
export function BLEProvider({ children }: { children: ReactNode }) {
  const [piStatus, setPiStatus] = useState<any>({});
  const [dbUpdateTrigger, setDbUpdateTrigger] = useState(0);
  const triggerDbUpdate = () => setDbUpdateTrigger(v => v + 1);

  function handleNotify(err: BleError | null, chr: Characteristic | null) {
    if (err || !chr?.value) return;
    const { type, json } = decode(chr.value);
    if (type === MESSAGE_TYPES.SUCCESS && json.connected) {
      setPiStatus(json);
      triggerDbUpdate();         // ← bump your DB trigger if you still need it
    }
  }

  const ble = useBLE(handleNotify);

  const value: BLECtx = {
    ...ble,
    piStatus,
    dbUpdateTrigger,
    triggerDbUpdate
  };

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}



/* ------------------------------------------------------------------ */
/*  3.  tiny helper hook                                              */
/* ------------------------------------------------------------------ */
export const useBLEContext = () => {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("BLEProvider missing higher in the tree");
  return ctx;
};
