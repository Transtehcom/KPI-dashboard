CREATE TABLE `dashboard_chunks` (
	`sandbox` text NOT NULL,
	`head` text NOT NULL,
	`part` integer NOT NULL,
	`payload` text NOT NULL,
	PRIMARY KEY(`sandbox`, `head`, `part`),
	FOREIGN KEY (`sandbox`) REFERENCES `dashboard_sandboxes`(`id`) ON UPDATE no action ON DELETE cascade
);
--> statement-breakpoint
CREATE TABLE `dashboard_sandboxes` (
	`id` text PRIMARY KEY NOT NULL,
	`owner` text NOT NULL,
	`revision` integer NOT NULL,
	`head` text NOT NULL,
	`expires` integer NOT NULL
);
--> statement-breakpoint
CREATE INDEX `dashboard_sandbox_expiry` ON `dashboard_sandboxes` (`expires`);