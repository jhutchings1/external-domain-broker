"""empty message

Revision ID: 120d4ff72a67
Revises: d2d0f4bf0778
Create Date: 2020-04-24 19:42:17.771161

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "120d4ff72a67"
down_revision = "d2d0f4bf0778"
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "operation",
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("service_instance_id", sa.String(), nullable=False),
        sa.Column(
            "state",
            sa.Enum("IN_PROGRESS", "SUCCEEDED", "FAILED", name="operationstate"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["service_instance_id"], ["service_instance.id"],),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("service_instance", schema=None) as batch_op:
        batch_op.drop_column("status")

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table("service_instance", schema=None) as batch_op:
        batch_op.add_column(sa.Column("status", sa.TEXT(), nullable=False))

    op.drop_table("operation")
    # ### end Alembic commands ###