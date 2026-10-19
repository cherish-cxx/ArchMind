package com.example.legacy.model;

public class Customer {

    private final long id;
    private final String name;
    private final boolean vip;

    public Customer(long id, String name, boolean vip) {
        this.id = id;
        this.name = name;
        this.vip = vip;
    }

    public long getId() {
        return id;
    }

    public String getName() {
        return name;
    }

    public boolean isVip() {
        return vip;
    }
}
