import { forwardRef, type ButtonHTMLAttributes } from "react";

export type OrbMode =
  | "idle"
  | "passive"
  | "listening"
  | "uploading"
  | "speaking"
  | "error";

export type OrbSize = "small" | "medium" | "large";

export interface OrbProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "type"> {
  mode: OrbMode;
  size?: OrbSize;
}

const Orb = forwardRef<HTMLButtonElement, OrbProps>(function Orb(
  { mode, size = "large", className, children, ...rest },
  ref,
) {
  const cls = `orb orb--${size} orb--${mode}${className ? ` ${className}` : ""}`;
  return (
    <button ref={ref} type="button" className={cls} {...rest}>
      <span className="orb-halo" aria-hidden />
      <span className="orb-aura" aria-hidden />
      <span className="orb-core" aria-hidden />
      <span className="orb-shine" aria-hidden />
      {children}
    </button>
  );
});

export default Orb;
