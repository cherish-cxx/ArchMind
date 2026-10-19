package com.example.demo.util;

import java.util.concurrent.atomic.AtomicLong;

public final class IdGenerator {

    private static final AtomicLong SEQUENCE = new AtomicLong(1000);

    private IdGenerator() {
    }

    public static long nextId() {
        return SEQUENCE.incrementAndGet();
    }
}
