import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { MoneyText, formatPaise } from '../src/components/MoneyText';

describe('MoneyText (docs/18 §3)', () => {
  it('formats paise as rupees in the Indian grouping, with paise', () => {
    expect(formatPaise(48_000_000)).toBe('₹4,80,000.00');
    expect(formatPaise(1_250_050)).toBe('₹12,500.50');
    expect(formatPaise(12_345_678_901)).toBe('₹12,34,56,789.01');
    expect(formatPaise(5)).toBe('₹0.05');
    expect(formatPaise(0)).toBe('₹0.00');
    expect(formatPaise(-1_999)).toBe('-₹19.99');
  });

  it('never goes through a float fraction', () => {
    // 0.07 * 100 is 7.000000000000001 in floating point; integer paise stay exact.
    expect(formatPaise(7)).toBe('₹0.07');
    expect(formatPaise(9_007_199_254_740_991)).toBe('₹9,00,71,99,25,47,409.91');
  });

  it('refuses anything that is not whole paise', () => {
    expect(() => formatPaise(1.5)).toThrow(RangeError);
  });

  it('renders the text', () => {
    render(<MoneyText paise={48_000_000} />);
    expect(screen.getByText('₹4,80,000.00')).toBeInTheDocument();
  });
});
