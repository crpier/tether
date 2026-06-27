import js from "@eslint/js";
import tseslint from "typescript-eslint";

export default tseslint.config(
  {
    ignores: ["dist/**", "node_modules/**"],
  },
  js.configs.recommended,
  ...tseslint.configs.strictTypeChecked,
  ...tseslint.configs.stylisticTypeChecked,
  {
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      "@typescript-eslint/no-confusing-void-expression": "off",
      "@typescript-eslint/no-non-null-assertion": "off",
    },
  },
  {
    files: ["src/generated/schema.ts"],
    rules: {
      "@typescript-eslint/consistent-indexed-object-style": "off",
    },
  },
  {
    files: ["eslint.config.js", "scripts/*.mjs"],
    languageOptions: {
      globals: { console: "readonly", process: "readonly" },
    },
    extends: [tseslint.configs.disableTypeChecked],
  },
);
