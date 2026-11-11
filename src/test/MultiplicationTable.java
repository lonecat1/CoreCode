import java.util.stream.IntStream;

public class MultiplicationTable {
    public static void main(String[] args) {
        IntStream.rangeClosed(1, 9).forEach(i -> {
            StringBuilder row = new StringBuilder();
            for (int j = 1; j <= i; j++) {
                if (j > 1) row.append(" ");
                row.append(j).append("x").append(i).append("=").append(String.format("%-2d", i * j));
            }
            System.out.println(row);
        });
    }
}
