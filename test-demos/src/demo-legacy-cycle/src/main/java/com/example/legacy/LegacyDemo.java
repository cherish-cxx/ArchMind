package com.example.legacy;

import com.example.legacy.model.Customer;
import com.example.legacy.model.Invoice;
import com.example.legacy.util.NumberChecks;

import java.util.List;

public class LegacyDemo {

    public static void main(String[] args) {
        Customer vip = new Customer(1, "Alice", true);
        Customer normal = new Customer(2, "Bob", false);

        Invoice first = new Invoice(1001, vip);
        first.addItem("book", 30.0, 2);
        first.addItem("pen", 5.0, 10);

        Invoice second = new Invoice(1002, normal);
        second.addItem("desk", 200.0, 1);

        LegacyBillingSystem system = new LegacyBillingSystem(new BillingCalculator(), new DiscountPolicy());

        System.out.println("version=" + LegacyBillingSystem.version());
        System.out.println("first=" + system.calculate(first));
        System.out.println("second=" + system.calculate(second));
        System.out.print(system.report(List.of(first, second)));
        System.out.println("even(10)=" + NumberChecks.isEven(10) + " odd(7)=" + NumberChecks.isOdd(7));
    }
}
