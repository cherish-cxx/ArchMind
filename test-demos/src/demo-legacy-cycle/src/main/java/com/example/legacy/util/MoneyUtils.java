package com.example.legacy.util;

public final class MoneyUtils {

    private MoneyUtils() {
    }

    public static double round(double value) {
        return Math.round(value * 100.0) / 100.0;
    }

    public static long power(long base, int exponent) {
        if (exponent <= 0) {
            return 1;
        }
        return base * power(base, exponent - 1);
    }
}
