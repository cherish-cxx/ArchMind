package com.example.archmind.model.ast;

/**
 * 方法索引键：所属类全限定名 + 方法名 + 参数个数，三元组定位一个方法。
 * record 自带 equals/hashCode，可直接当 Map 的 key。
 * 同参数个数的重载（save(String) vs save(int)）P1 区分不了，接受。
 */
public record MethodKey(String owner, String name, int argCount) {
}
