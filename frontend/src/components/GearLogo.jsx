function GearLogo({ size = 90 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 100 100">
      <g fill="#0868f9">
        {[0, 45, 90, 135].map((angle) => (
          <rect
            key={angle}
            x="40"
            y="2"
            width="20"
            height="96"
            rx="5"
            transform={`rotate(${angle} 50 50)`}
          />
        ))}
        <circle cx="50" cy="50" r="38" />
      </g>
      <circle cx="50" cy="50" r="24" fill="white" />
      <circle cx="50" cy="50" r="16" fill="#0868f9" />
      <circle cx="50" cy="50" r="6" fill="white" />
      <path
        d="M50 50 L63 33"
        stroke="white"
        strokeWidth="5"
        strokeLinecap="round"
      />
    </svg>
  );
}

export default GearLogo;