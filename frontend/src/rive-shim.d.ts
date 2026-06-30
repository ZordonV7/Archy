// Ambient shim so tsc can type-check the frontend even when
// @rive-app/react-canvas is not installed in CI / sandboxed environments.
// The real types come from the package itself once it's `npm install`ed.
declare module "@rive-app/react-canvas" {
  import type { ComponentType } from "react";
  interface UseRiveOptions {
    src: string;
    stateMachines?: string | string[];
    autoplay?: boolean;
    [key: string]: unknown;
  }
  interface UseRiveResult {
    rive: unknown;
    RiveComponent: ComponentType<Record<string, unknown>> | null;
  }
  export function useRive(opts: UseRiveOptions): UseRiveResult;
  export function useStateMachineInput(
    rive: unknown,
    stateMachineName: string,
    inputName: string
  ): { value: string | number | boolean } | null;
}
