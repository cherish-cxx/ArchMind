package com.example.legacy.util;

public final class NumberChecks {

    private NumberChecks() {
    }

    public static boolean isEven(int value) {
        if (value == 0) {
            return true;
        }
        if (value < 0) {
            return isEven(-value);
        }
        return isOdd(value - 1);
    }

    public static boolean isOdd(int value) {
        if (value == 0) {
            return false;
        }
        if (value < 0) {
            return isOdd(-value);
        }
        return isEven(value - 1);
    }
}
