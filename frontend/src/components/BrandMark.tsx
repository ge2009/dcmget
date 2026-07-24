type BrandMarkProps = {
  className?: string;
  label?: string;
};

/** Shared application mark. The backend serves the packaged icon at /favicon.ico. */
export function BrandMark({ className = '', label }: BrandMarkProps) {
  return <span
    className={`brand-mark ${className}`.trim()}
    role={label ? 'img' : undefined}
    aria-label={label}
    aria-hidden={label ? undefined : true}
  >
    <img src="/favicon.ico" alt="" draggable={false} />
  </span>;
}
