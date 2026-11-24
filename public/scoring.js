export const points = [0, 10, 15, 60, 85, 100];
export const metres = [0, 50, 300, 1200, 4100, 7800];
export const depthNames = [
  "Miss",
  "Sunlit",
  "Twilight",
  "Midnight",
  "Abyss",
  "Trench",
];
export const depthLabel = (tier) => metres[tier].toLocaleString("en-GB") + " m";
