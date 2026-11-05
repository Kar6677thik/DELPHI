import js from '@eslint/js';
import { defineConfig } from 'eslint/config';
import reactHooks from 'eslint-plugin-react-hooks';
import globals from 'globals';
import tseslint from 'typescript-eslint';

export default defineConfig(
  { ignores: ['dist', 'coverage', 'mock-public', 'src/api/schema.d.ts'] },
  js.configs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    extends: [tseslint.configs.strictTypeChecked, reactHooks.configs.flat['recommended-latest']],
    languageOptions: {
      globals: globals.browser,
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
    },
    rules: {
      '@typescript-eslint/no-explicit-any': 'error',
      'no-restricted-syntax': [
        'error',
        {
          selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
          message: 'dangerouslySetInnerHTML is banned (docs/19 §4).',
        },
      ],
    },
  },
  { files: ['*.js'], languageOptions: { globals: globals.node } },
);
