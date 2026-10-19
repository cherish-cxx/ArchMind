package com.example.legacy.model;

import java.util.ArrayList;
import java.util.List;

public class Invoice {

    private final long id;
    private final Customer customer;
    private final List<LineItem> items = new ArrayList<>();

    public Invoice(long id, Customer customer) {
        this.id = id;
        this.customer = customer;
    }

    public void addItem(String name, double unitPrice, int quantity) {
        items.add(new LineItem(name, unitPrice, quantity));
    }

    public long getId() {
        return id;
    }

    public Customer getCustomer() {
        return customer;
    }

    public List<LineItem> getItems() {
        return items;
    }

    public static class LineItem {

        private final String name;
        private final double unitPrice;
        private final int quantity;

        public LineItem(String name, double unitPrice, int quantity) {
            this.name = name;
            this.unitPrice = unitPrice;
            this.quantity = quantity;
        }

        public String getName() {
            return name;
        }

        public double getUnitPrice() {
            return unitPrice;
        }

        public int getQuantity() {
            return quantity;
        }

        public double getSubtotal() {
            return unitPrice * quantity;
        }
    }
}
